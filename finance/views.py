from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone as tz
from rest_framework import viewsets, filters, status
from notifications.dispatcher import send_notification
from notifications.models import Notification
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from users.permissions import RoleScopedPermission
from inventory.models import StockLevel
from .models import (
    SalesInvoice, Payment, PaymentMethod,
    PurchaseInvoice, SupplierPayment,
    Expense, ExpenseCategory,
    WorkShift,
    RefundInvoice,
)
from .serializers import (
    SalesInvoiceSerializer, PaymentSerializer, PaymentMethodSerializer,
    PurchaseInvoiceSerializer, SupplierPaymentSerializer,
    ExpenseSerializer, ExpenseCategorySerializer,
    WorkShiftSerializer,
    RefundInvoiceSerializer,
)
from core.activity import log_activity
from core.models import ActivityLog, Branch


class PaymentMethodViewSet(viewsets.ModelViewSet):
    serializer_class = PaymentMethodSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'ADMIN',
        'update':         'ADMIN',
        'partial_update': 'ADMIN',
        'destroy':        'ADMIN',
    }

    def get_queryset(self):
        return PaymentMethod.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class SalesInvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = SalesInvoiceSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'CASHIER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'MANAGER',
        'void':           'MANAGER',
        'checkout':       'CASHIER',
        'print_data':     'CASHIER',
    }
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['date', 'created_at', 'grand_total', 'invoice_number']

    def get_queryset(self):
        return SalesInvoice.objects.filter(
            store=self.request.user.store
        ).prefetch_related('items', 'payments').select_related('customer', 'branch')

    def perform_create(self, serializer):
        from billing.quota import enforce_quota
        enforce_quota(self.request.user.store, 'invoices')
        invoice = serializer.save(store=self.request.user.store)
        log_activity(
            request=self.request,
            action=f"Created Sales Invoice #{invoice.invoice_number}",
            op_type=ActivityLog.OperationType.SALE,
            details={
                'invoice_id': str(invoice.id),
                'invoice_number': invoice.invoice_number,
                'grand_total': str(invoice.grand_total),
                'customer': invoice.customer.name if invoice.customer else None,
                'status': invoice.status,
            },
        )

    @action(detail=True, methods=['post'])
    def void(self, request, pk=None):
        invoice = self.get_object()
        if invoice.status == SalesInvoice.Status.VOID:
            return Response({'detail': 'Already voided.'}, status=status.HTTP_400_BAD_REQUEST)
        invoice.status = SalesInvoice.Status.VOID
        invoice.save()
        send_notification(
            store=invoice.store,
            title=f"Invoice #{invoice.invoice_number} was voided",
            body=f"Total: {invoice.grand_total}",
            priority=Notification.Priority.ALERT,
            notif_type=Notification.Type.INVOICE_VOIDED,
            link="/finance/invoices",
        )
        log_activity(
            request=request,
            action=f"Voided Sales Invoice #{invoice.invoice_number}",
            op_type=ActivityLog.OperationType.SALE,
            details={
                'invoice_id': str(invoice.id),
                'invoice_number': invoice.invoice_number,
                'grand_total': str(invoice.grand_total),
            },
        )
        return Response(SalesInvoiceSerializer(invoice).data)

    @action(detail=True, methods=['post'])
    def checkout(self, request, pk=None):
        """Finalize a DRAFT sale: enforce store policies, then flip DRAFT→POSTED
        in one atomic step so the existing stock-out + invoice-numbering signals
        fire. CASHIER-allowed — unlike a raw PATCH to POSTED, which is MANAGER-only.
        This is the canonical 'complete the sale' call the POS till makes."""
        invoice = self.get_object()
        if invoice.status == SalesInvoice.Status.POSTED:
            return Response({'detail': 'Invoice already posted.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if invoice.status == SalesInvoice.Status.VOID:
            return Response({'detail': 'Cannot post a voided invoice.'},
                            status=status.HTTP_400_BAD_REQUEST)

        items = list(invoice.items.select_related('variant__product').all())
        if not items:
            return Response({'detail': 'Cannot post an empty invoice.'},
                            status=status.HTTP_400_BAD_REQUEST)

        settings_obj = getattr(invoice.store, 'settings', None)

        # Policy 1 — credit (agel) selling. Block posting an unpaid invoice when
        # the owner disabled credit. No race here (reads invoice totals only).
        allow_agel = getattr(settings_obj, 'enable_agel_selling', True)
        if not allow_agel and (invoice.grand_total - invoice.paid_amount) > 0:
            return Response(
                {'detail': 'Credit sales are disabled for this store. '
                           'Collect full payment before completing the sale.'},
                status=status.HTTP_400_BAD_REQUEST)

        # Policy 3 — credit limit. Now that this sale is about to extend credit
        # (if unpaid), enforce the store's ALLOW/WARN/BLOCK policy against the
        # customer's REAL outstanding balance. BLOCK raises → stays DRAFT.
        from .serializers import enforce_credit_policy
        enforce_credit_policy(invoice)

        allow_negative = getattr(settings_obj, 'allow_negative_stock', False)

        with transaction.atomic():
            # Policy 2 — overselling. Lock the stock rows we're about to move and
            # verify availability INSIDE the transaction, so a concurrent checkout
            # can't slip between the check and the signal's decrement.
            if not allow_negative:
                shortages = []
                for item in items:
                    stock = (StockLevel.objects.select_for_update()
                             .filter(variant=item.variant, branch=invoice.branch).first())
                    available = stock.quantity if stock else Decimal('0')
                    # Compare in BASE units: a line of 1 Pack consumes factor base units.
                    base_qty = Decimal(str(item.quantity)) * Decimal(str(item.unit_factor or 1))
                    if base_qty > available:
                        shortages.append({
                            'variant': str(item.variant.id),
                            'sku': item.variant.sku,
                            'name': item.variant.product.name,
                            'requested': str(base_qty),
                            'available': str(available),
                        })
                if shortages:
                    raise ValidationError({
                        'detail': 'Insufficient stock to complete this sale.',
                        'shortages': shortages,
                    })

            # Policy 4 — expired stock (FEFO). When the store blocks expired sales,
            # peek at the batches this checkout would draw (earliest-expiry first) and
            # reject if covering the line forces a draw from an expired batch. WARN/
            # ALLOW fall through (the POS surfaces the warning pre-checkout).
            expired_policy = getattr(settings_obj, 'expired_sale_policy', 'WARN')
            if expired_policy == 'BLOCK':
                from inventory.models import is_expiry_tracked, StockBatch
                from django.db.models import F
                from django.utils import timezone as _tz
                today = _tz.now().date()
                blocked = []
                for item in items:
                    if not is_expiry_tracked(item.variant):
                        continue
                    need = Decimal(str(item.quantity)) * Decimal(str(item.unit_factor or 1))
                    batches = (StockBatch.objects.select_for_update()
                               .filter(variant=item.variant, branch=invoice.branch,
                                       quantity_remaining__gt=0)
                               .order_by(F('expiry_date').asc(nulls_last=True), 'received_date'))
                    for b in batches:
                        if need <= 0:
                            break
                        take = min(Decimal(str(b.quantity_remaining)), need)
                        if b.expiry_date and b.expiry_date < today:
                            blocked.append({
                                'variant': str(item.variant.id),
                                'sku': item.variant.sku,
                                'name': item.variant.product.name,
                                'expiry_date': b.expiry_date.isoformat(),
                            })
                            break
                        need -= take
                if blocked:
                    raise ValidationError({
                        'detail': 'This sale would draw from expired stock, which this store blocks.',
                        'expired': blocked,
                    })

            # Flip to POSTED → fires handle_sale_stock (decrement + COGS snapshot)
            # and assigns the human-readable invoice_number.
            invoice.status = SalesInvoice.Status.POSTED
            invoice.save()

        log_activity(
            request=request,
            action=f"Completed Sales Invoice #{invoice.invoice_number}",
            op_type=ActivityLog.OperationType.SALE,
            details={
                'invoice_id': str(invoice.id),
                'invoice_number': invoice.invoice_number,
                'grand_total': str(invoice.grand_total),
                'paid_amount': str(invoice.paid_amount),
            },
        )
        return Response(SalesInvoiceSerializer(invoice).data)

    @action(detail=True, methods=['get'], url_path='print-data')
    def print_data(self, request, pk=None):
        """Fully-resolved payload for the printable invoice — store header,
        legal info (Tax ID gated by StoreSettings.print_tax_id), customer,
        line items with product names, and payment breakdown. One round-trip
        so the print view never has to stitch lookups together."""
        invoice = self.get_object()
        store = invoice.store
        settings_obj = getattr(store, 'settings', None)

        currency = None
        if getattr(store, 'currency', None):
            currency = {
                'symbol': store.currency.symbol,
                'position': store.currency.position,
            }

        items = [{
            'name': item.variant.product.name,
            'sku': item.variant.sku,
            'quantity': str(item.quantity),
            'unit_price': str(item.unit_price),
            'tax_amount': str(item.tax_amount),
            'total': str(item.total),
        } for item in invoice.items.select_related('variant__product').all()]

        payments = [{
            'method': p.method.name,
            'amount': str(p.amount),
        } for p in invoice.payments.select_related('method').all()]

        # Tax ID only travels to the client when the store opts in — off means
        # it is omitted entirely, not blanked to "N/A".
        show_tax_id = bool(settings_obj and settings_obj.print_tax_id)

        return Response({
            'invoice': {
                'invoice_number': invoice.invoice_number,
                'status': invoice.status,
                'date': invoice.date,
                'subtotal': str(invoice.subtotal),
                'tax_total': str(invoice.tax_total),
                'discount': str(invoice.discount),
                'grand_total': str(invoice.grand_total),
                'paid_amount': str(invoice.paid_amount),
            },
            'store': {
                'name': store.name,
                'phone_number': getattr(store, 'phone_number', '') or '',
                'city': getattr(store, 'city', '') or '',
                'country': getattr(store, 'country', '') or '',
            },
            'branch': {'name': invoice.branch.name} if invoice.branch else None,
            'customer': {'name': invoice.customer.name} if invoice.customer else None,
            'legal': {
                'tax_id': settings_obj.tax_id if (show_tax_id and settings_obj) else '',
                'show_tax_id': show_tax_id,
                'commercial_reg': settings_obj.commercial_reg if settings_obj else '',
                'receipt_header': settings_obj.receipt_header if settings_obj else '',
                'receipt_footer': settings_obj.receipt_footer if settings_obj else '',
            },
            'currency': currency,
            'items': items,
            'payments': payments,
        })


class PaymentViewSet(viewsets.ModelViewSet):
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':     'CASHIER',
        'retrieve': 'CASHIER',
        'create':   'CASHIER',
    }
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        return Payment.objects.filter(invoice__store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class PurchaseInvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = PurchaseInvoiceSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'MANAGER',
        'retrieve':       'MANAGER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
        'receive':        'MANAGER',
        'label_data':     'MANAGER',
    }

    def get_queryset(self):
        qs = PurchaseInvoice.objects.filter(
            store=self.request.user.store
        ).prefetch_related('items').select_related('supplier', 'branch')
        p = self.request.query_params
        if q := p.get('q'):
            qs = qs.filter(
                models.Q(vendor_reference__icontains=q) |
                models.Q(supplier__name__icontains=q)
            )
        if s := p.get('status'):
            qs = qs.filter(status=s)
        if sup := p.get('supplier'):
            qs = qs.filter(supplier_id=sup)
        if date_from := p.get('date_from'):
            qs = qs.filter(date__date__gte=date_from)
        if date_to := p.get('date_to'):
            qs = qs.filter(date__date__lte=date_to)
        return qs

    def perform_create(self, serializer):
        # Resolve branch server-side: prefer a submitted valid branch, else the
        # user's default branch, else the store's first branch.
        store = self.request.user.store
        branch = serializer.validated_data.get('branch')
        if branch is None or branch.store_id != store.id:
            branch = getattr(self.request.user, 'default_branch', None)
        if branch is None or branch.store_id != store.id:
            branch = Branch.objects.filter(store=store).order_by('created_at').first()
        purchase = serializer.save(store=store, branch=branch)
        log_activity(
            request=self.request,
            action=f"Created Purchase {purchase.purchase_number or purchase.vendor_reference or str(purchase.id)[:8]}",
            op_type=ActivityLog.OperationType.PURCHASE,
            details={
                'purchase_id': str(purchase.id),
                'vendor_reference': purchase.vendor_reference or '',
                'supplier': purchase.supplier.name if purchase.supplier else None,
                'grand_total': str(purchase.total_amount),
                'status': purchase.status,
            },
        )

    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        invoice = self.get_object()
        if invoice.status != PurchaseInvoice.Status.DRAFT:
            return Response(
                {'detail': 'Only DRAFT invoices can be received.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not invoice.supplier_id:
            return Response(
                {'detail': 'A supplier must be assigned before receiving stock.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        invoice.status = PurchaseInvoice.Status.RECEIVED
        invoice.save()
        log_activity(
            request=request,
            action=f"Received Purchase {invoice.purchase_number or str(invoice.id)[:8]}",
            op_type=ActivityLog.OperationType.PURCHASE,
            details={
                'purchase_id': str(invoice.id),
                'purchase_number': invoice.purchase_number,
                'supplier': invoice.supplier.name if invoice.supplier else None,
            },
        )
        return Response(PurchaseInvoiceSerializer(invoice).data)

    @action(detail=True, methods=['get'], url_path='label-data')
    def label_data(self, request, pk=None):
        """Returns fully-resolved label payload for all items in this purchase."""
        invoice = self.get_object()
        store = invoice.store
        items = []
        for item in invoice.items.select_related('variant__product'):
            v = item.variant
            items.append({
                'variant_id':   str(v.id),
                'product_name': v.product.name,
                'sku':          v.sku,
                'barcode':      v.barcode or v.sku,
                'sell_price':   str(v.sell_price),
                'quantity':     int(item.quantity),
            })
        return Response({'store_name': store.name, 'items': items})


class ExpenseCategoryViewSet(viewsets.ModelViewSet):
    serializer_class = ExpenseCategorySerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'MANAGER',
        'retrieve':       'MANAGER',
        'create':         'ADMIN',
        'update':         'ADMIN',
        'partial_update': 'ADMIN',
        'destroy':        'ADMIN',
    }

    def get_queryset(self):
        return ExpenseCategory.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class ExpenseViewSet(viewsets.ModelViewSet):
    serializer_class = ExpenseSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'MANAGER',
        'retrieve':       'MANAGER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
    }
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['date', 'amount']

    def get_queryset(self):
        return Expense.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        expense = serializer.save(store=self.request.user.store)
        log_activity(
            request=self.request,
            action=f"Recorded Expense: {expense.description or expense.category.name}",
            op_type=ActivityLog.OperationType.EXPENSE,
            details={
                'expense_id': str(expense.id),
                'amount': str(expense.amount),
                'category': expense.category.name if expense.category else None,
            },
        )


class SupplierPaymentViewSet(viewsets.ModelViewSet):
    """Payments made to a supplier (running-account model). Filter the list by
    ?supplier=<uuid> for a supplier's payment history."""
    serializer_class = SupplierPaymentSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'MANAGER',
        'retrieve':       'MANAGER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
    }
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['date', 'amount']
    ordering = ['-date']

    def get_queryset(self):
        qs = SupplierPayment.objects.filter(store=self.request.user.store).select_related('created_by')
        supplier = self.request.query_params.get('supplier')
        if supplier:
            qs = qs.filter(supplier_id=supplier)
        return qs

    def perform_create(self, serializer):
        payment = serializer.save(store=self.request.user.store, created_by=self.request.user)
        log_activity(
            request=self.request,
            action=f"Recorded Supplier Payment: {payment.amount} → {payment.supplier.name}",
            op_type=ActivityLog.OperationType.EXPENSE,
            details={
                'supplier_payment_id': str(payment.id),
                'supplier': payment.supplier.name,
                'amount': str(payment.amount),
                'method': payment.method,
            },
        )


class WorkShiftViewSet(viewsets.ModelViewSet):
    serializer_class = WorkShiftSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'CASHIER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
        'close':          'CASHIER',
        'summary':        'CASHIER',
    }

    def get_queryset(self):
        qs = WorkShift.objects.filter(store=self.request.user.store)
        shift_status = self.request.query_params.get('status')
        if shift_status:
            qs = qs.filter(status=shift_status)
        return qs

    def perform_create(self, serializer):
        open_shift = WorkShift.objects.filter(
            store=self.request.user.store,
            user=self.request.user,
            status=WorkShift.Status.OPEN,
        ).first()
        if open_shift:
            raise ValidationError('You already have an open shift.')
        shift = serializer.save(store=self.request.user.store, user=self.request.user)
        log_activity(
            request=self.request,
            action="Opened Shift",
            op_type=ActivityLog.OperationType.SHIFT,
            details={
                'shift_id': str(shift.id),
                'starting_cash': str(shift.starting_cash),
            },
        )

    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        shift = self.get_object()
        if shift.status == WorkShift.Status.CLOSED:
            return Response({'detail': 'Shift already closed.'}, status=status.HTTP_400_BAD_REQUEST)
        counted_cash = request.data.get('counted_cash', 0)
        shift.close_shift(counted_cash)
        if shift.difference != 0:
            direction = "over" if shift.difference > 0 else "short"
            send_notification(
                store=shift.store,
                title=f"Shift closed with cash {direction}",
                body=f"Cashier: {shift.user.get_full_name() or shift.user.username} · Difference: {shift.difference:+.2f}",
                priority=Notification.Priority.WARNING,
                notif_type=Notification.Type.SHIFT_DIFFERENCE,
                link="/finance/shifts",
            )
        log_activity(
            request=request,
            action="Closed Shift",
            op_type=ActivityLog.OperationType.SHIFT,
            details={
                'shift_id': str(shift.id),
                'starting_cash': str(shift.starting_cash),
                'counted_cash': str(counted_cash),
                'expected_cash': str(getattr(shift, 'expected_cash', '')),
            },
        )
        return Response(WorkShiftSerializer(shift).data)

    @action(detail=True, methods=['get'])
    def summary(self, request, pk=None):
        from django.db.models import Sum
        shift = self.get_object()
        end = shift.end_time or tz.now()
        invoices_qs = (SalesInvoice.objects
                       .filter(store=request.user.store,
                               branch=shift.branch,
                               is_deleted=False,
                               status=SalesInvoice.Status.POSTED,
                               created_at__gte=shift.start_time,
                               created_at__lte=end))
        total_sales = invoices_qs.aggregate(t=Sum('grand_total'))['t'] or Decimal('0')
        invoice_count = invoices_qs.count()

        payments_qs = (Payment.objects
                       .filter(invoice__in=invoices_qs)
                       .values('method__name')
                       .annotate(total=Sum('amount'))
                       .order_by('-total'))
        payment_breakdown = [{'method': p['method__name'] or 'Unknown', 'total': str(p['total'])} for p in payments_qs]

        invoice_list = list(invoices_qs.values('id', 'invoice_number', 'customer__name', 'grand_total', 'paid_amount', 'created_at').order_by('-created_at')[:50])
        for inv in invoice_list:
            inv['id'] = str(inv['id'])
            inv['grand_total'] = str(inv['grand_total'])
            inv['paid_total'] = str(inv['paid_amount'])
            inv['customer'] = inv.pop('customer__name') or 'Walk-in'
            inv['created_at'] = inv['created_at'].isoformat() if inv['created_at'] else None

        return Response({
            'shift': WorkShiftSerializer(shift).data,
            'invoice_count': invoice_count,
            'total_sales': str(total_sales),
            'payment_breakdown': payment_breakdown,
            'invoices': invoice_list,
        })


class RefundInvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = RefundInvoiceSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
    }

    def get_queryset(self):
        return RefundInvoice.objects.filter(
            store=self.request.user.store
        ).prefetch_related('items').select_related('customer', 'branch', 'original_invoice')

    def perform_create(self, serializer):
        refund = serializer.save(store=self.request.user.store, created_by=self.request.user)
        log_activity(
            request=self.request,
            action=f"Created Return #{refund.refund_number or refund.id}",
            op_type=ActivityLog.OperationType.RETURN,
            details={
                'refund_id': str(refund.id),
                'original_invoice': refund.original_invoice.invoice_number if refund.original_invoice else None,
                'total': str(refund.total_refunded),
            },
        )
