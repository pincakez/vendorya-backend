from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models, transaction
from django.utils import timezone as tz
from rest_framework import viewsets, filters, status
from notifications.dispatcher import send_notification, msg
from notifications.models import Notification
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from users.models import User
from users.permissions import RoleScopedPermission, ROLE_RANK, _user_rank
from rest_framework.exceptions import PermissionDenied
from inventory.models import StockLevel
from .models import (
    SalesInvoice, Payment, PaymentMethod, CustomerReceipt,
    PurchaseInvoice, SupplierPayment, PurchaseReturn, Cheque,
    Expense, ExpenseCategory,
    WorkShift,
    RefundInvoice,
)
from .serializers import (
    SalesInvoiceSerializer, PaymentSerializer, PaymentMethodSerializer, CustomerReceiptSerializer,
    PurchaseInvoiceSerializer, SupplierPaymentSerializer, PurchaseReturnSerializer, ChequeSerializer,
    ExpenseSerializer, ExpenseCategorySerializer,
    WorkShiftSerializer,
    RefundInvoiceSerializer,
)
from core.activity import log_activity
from core.models import ActivityLog, Branch


def require_open_shift(store, user, branch):
    """Yakot 2026-10-14: when the shop turned on "Require an open shift to sell", a sale is completed only by
    someone with their OWN open shift (drawer) in the sale's branch — the drawer that will count its cash.
    Raises a 400 the till recognises by `code` (to offer "Open shift"); does nothing when the setting is off."""
    if not getattr(getattr(store, 'settings', None), 'require_shift_to_sell', False):
        return
    if WorkShift.objects.filter(store=store, user=user, branch=branch, status=WorkShift.Status.OPEN).exists():
        return
    raise ValidationError({'detail': 'Open your shift first — this shop sells only with an open shift.',
                           'code': 'shift_required'})


def refuse_void_with_returns(invoice):
    """s168 (surgeon-reviewer): a POSTED sale that already has a live return can't be voided — the void would put
    the FULL quantity back on the shelf although the return already restocked part of it (stock counted twice),
    and the return's money would hang off a cancelled sale. Fix the return, or return the rest instead."""
    from .models import RefundInvoice
    if invoice.status == SalesInvoice.Status.POSTED and RefundInvoice.objects.filter(
            original_invoice=invoice, is_deleted=False).exists():
        raise ValidationError({'detail': 'This sale already has a return — it can\'t be voided. Return the rest instead.',
                               'code': 'void_has_returns'})
    # s172: a sale made from a reservation carries the deposit as payments with no box line of their own — voiding it
    # would leave that money unexplained. Return the items instead (the return pays the money back the normal way).
    if invoice.status == SalesInvoice.Status.POSTED and invoice.payments.filter(deposit_of__isnull=False).exists():
        raise ValidationError({'detail': 'This sale was made from a reservation — it can\'t be voided. Return the items instead.',
                               'code': 'void_from_reservation'})
    # PROLINE-IMPORT-B.md: a sale imported from the old system moved no stock and no money here — a void would put its
    # goods back on today's shelf. Return the items instead (a return restocks what really comes back).
    if invoice.status == SalesInvoice.Status.POSTED and invoice.source_number:
        raise ValidationError({'detail': 'This sale was imported from the old system — it can\'t be voided. '
                                         'Return the items instead.', 'code': 'void_imported'})


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
        # s167: the system "Store credit" method stays out of the list — the till spends store credit with a
        # STORE_CREDIT payment line, and the shop can't rename / delete / pick it by hand.
        return PaymentMethod.objects.filter(store=self.request.user.store, is_store_credit=False)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class SalesInvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = SalesInvoiceSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'CASHIER',
        # update / partial_update / void: CASHIER may act on a DRAFT (the till's live cart); a POSTED sale
        # still needs MANAGER — enforced in _guard_posted below (s156). Before, the till's cart sync was
        # refused for cashiers, so checkout completed a stale cart.
        'update':         'CASHIER',
        'partial_update': 'CASHIER',
        'destroy':        'MANAGER',
        'void':           'CASHIER',
        'checkout':       'CASHIER',
        'print_data':     'CASHIER',
    }
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['date', 'created_at', 'grand_total', 'invoice_number']

    def perform_destroy(self, instance):
        # Only a draft cart may be deleted. A POSTED / VOID sale moved stock and money — deleting it would hide it
        # without giving the stock back or reversing its payments and money-box lines (s169 review). Void it instead.
        if instance.status != SalesInvoice.Status.DRAFT:
            raise ValidationError({'code': 'void_not_delete',
                                   'detail': 'A completed sale cannot be deleted — void it instead.'})
        instance.delete()

    def get_queryset(self):
        qs = SalesInvoice.objects.filter(
            store=self.request.user.store
        ).prefetch_related('items', 'payments').select_related('customer', 'branch')
        # s168: the Sales Invoices page and the Returns window send ?status= and ?search= — they were ignored
        # (Returns offered DRAFT carts as "original invoices"). Only on the list; detail/actions are unaffected.
        if self.action == 'list':
            params = self.request.query_params
            status_ = (params.get('status') or '').upper()
            if status_ in SalesInvoice.Status.values:
                qs = qs.filter(status=status_)
            term = (params.get('search') or '').strip()
            if term:
                q = models.Q(customer__name__icontains=term) | models.Q(customer__phone_number__icontains=term)
                if term.lstrip('#').isdigit() and len(term.lstrip('#')) <= 9:   # fits the integer column
                    q |= models.Q(invoice_number=int(term.lstrip('#')))
                q |= models.Q(source_number=term.lstrip('#'))   # an imported sale's old-system number
                qs = qs.filter(q)
        return qs

    def _is_manager(self):
        return _user_rank(self.request.user) >= ROLE_RANK[User.Role.MANAGER]

    def _guard_posted(self, invoice, new_status=None):
        """Below MANAGER you may only touch a DRAFT, and never move it out of DRAFT by editing."""
        if self._is_manager():
            return
        if invoice.status != SalesInvoice.Status.DRAFT:
            raise PermissionDenied('Only a manager can change a completed sale.')
        if new_status and new_status != SalesInvoice.Status.DRAFT:
            raise PermissionDenied('Complete the sale with checkout, not by editing it.')

    def perform_update(self, serializer):
        new_status = serializer.validated_data.get('status')
        self._guard_posted(serializer.instance, new_status)
        if new_status == SalesInvoice.Status.VOID:
            refuse_void_with_returns(serializer.instance)
        if new_status == SalesInvoice.Status.POSTED and serializer.instance.status != SalesInvoice.Status.POSTED:
            # A manager posting a draft by edit is a sale too — same shift rule as the till (2026-10-14).
            branch = serializer.validated_data.get('branch') or serializer.instance.branch
            require_open_shift(self.request.user.store, self.request.user, branch)
            from billing.quota import enforce_quota
            enforce_quota(self.request.user.store, 'invoices')   # a manager posting by edit uses the quota too (s166)
        was_posted = serializer.instance.status == SalesInvoice.Status.POSTED
        with transaction.atomic():
            invoice = serializer.save()
            if was_posted and invoice.status == SalesInvoice.Status.VOID:
                from .treasury_flows import reverse_sale   # TREASURY.md: the voided sale's money leaves its boxes
                reverse_sale(invoice, self.request.user)

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
        self._guard_posted(invoice)
        refuse_void_with_returns(invoice)
        was_draft = invoice.status == SalesInvoice.Status.DRAFT
        with transaction.atomic():
            invoice.status = SalesInvoice.Status.VOID
            invoice.save()
            if not was_draft:
                # TREASURY.md: the money this sale took goes back out of the boxes it went into (the customer
                # gets it back). A cash box that hasn't got it → 400 not_enough_money and the void is undone.
                from .treasury_flows import reverse_sale
                reverse_sale(invoice, request.user)
                # A repair job billed by this sale goes back to OPEN (s170): its bill is gone, so it can be billed
                # again with the normal Done button. The voided sale stays in the job's timeline as a note.
                from services.models import Service, ServiceNote
                for job in Service.objects.select_for_update().filter(store=invoice.store, invoice=invoice,
                                                                      status=Service.Status.DONE):
                    job.status, job.invoice = Service.Status.OPEN, None
                    job.save(update_fields=['status', 'invoice', 'updated_at'])
                    ServiceNote.objects.create(store=invoice.store, service=job, user=request.user,
                                               body=f'Invoice #{invoice.invoice_number} voided — job reopened.')
        if was_draft:
            # Discarding an unfinished cart (till hold/resume) is routine — no alert, no log noise (s156).
            return Response(SalesInvoiceSerializer(invoice).data)
        send_notification(
            store=invoice.store,
            title=f"Invoice #{invoice.invoice_number} was voided",
            body=f"Total: {invoice.grand_total}",
            priority=Notification.Priority.ALERT,
            notif_type=Notification.Type.INVOICE_VOIDED,
            link="/finance/invoices",
            i18n=msg('invoice_voided.title', 'invoice_voided.body',   # s167
                     number=invoice.invoice_number or '—', total=invoice.grand_total),
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
        idem_key = (request.headers.get('Idempotency-Key') or '').strip()[:64]
        if invoice.status == SalesInvoice.Status.POSTED:
            if idem_key and invoice.checkout_key == idem_key:
                return Response(SalesInvoiceSerializer(invoice).data)   # retry of a sale that already went through
            return Response({'detail': 'Invoice already posted.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if invoice.status == SalesInvoice.Status.VOID:
            return Response({'detail': 'Cannot post a voided invoice.'},
                            status=status.HTTP_400_BAD_REQUEST)

        items = list(invoice.items.select_related('variant__product').all())
        if not items:
            return Response({'detail': 'Cannot post an empty invoice.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Every policy + the post itself: finance/checkout.py (shared with a reservation becoming a sale, s172).
        from .checkout import post_sale
        plan = post_sale(invoice, request.user, request.data, idem_key)
        if plan is None:   # this exact checkout already went through (same Idempotency-Key, two requests at once)
            return Response(SalesInvoiceSerializer(invoice).data)
        log_activity(
            request=request,
            action=f"Completed Sales Invoice #{invoice.invoice_number}",
            op_type=ActivityLog.OperationType.SALE,
            details={
                'invoice_id': str(invoice.id),
                'invoice_number': invoice.invoice_number,
                'grand_total': str(invoice.grand_total),
                'paid_amount': str(invoice.paid_amount),
                'payments': [{'method': m.name, 'amount': str(a)} for m, a in plan.lines],
                'change': str(plan.change),
                'on_account': str(max(invoice.grand_total - invoice.paid_amount, Decimal('0'))),
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

        # §WT: a weight line (per-kg product, master switch on, base unit) is flagged so
        # the receipt shows "250 g" / "0.25 kg" in the store's default weight unit.
        # quantity stays the stored kg — the unit is display only.
        from inventory.models import is_weight_selling_enabled
        weight_on = is_weight_selling_enabled(store.id)
        items = [{
            'variant': str(item.variant_id),                    # s168: the Returns screen picks lines by these
            'unit': str(item.unit_id) if item.unit_id else None,
            'name': item.variant.product.name,
            'sku': item.variant.sku,
            'quantity': str(item.quantity),
            'is_weight': bool(weight_on and not item.unit_id
                              and item.variant.product.selling_mode == 'WEIGHT'),
            'unit_name': item.unit.name if item.unit_id else None,  # base unit → None (plain products unchanged)
            'unit_price': str(item.unit_price),
            'discount_amount': str(item.discount_amount),   # s165 (§AUDIT A19): the receipt shows "Disc: − X"
            'tax_amount': str(item.tax_amount),
            'total': str(item.total),
            # New 7/8 — printed only when present. condition = a code (NEW / IMPORTED_INSPECTED / USED) the
            # receipt words in the shop's language; on a DRAFT these are still empty (frozen at checkout).
            'serial_numbers': list(item.serial_numbers or []),
            'track_serials': bool(item.variant.product.track_serials),   # the Returns screen asks for serials
            'condition': item.condition or '',
            'warranty_months': item.warranty_months or 0,
            'warranty_ends_on': item.warranty_ends_on.isoformat() if item.warranty_ends_on else None,
        } for item in invoice.items.select_related('variant__product', 'unit').all()]

        # s167: one line per tender (split payment) — the flags let the receipt word them in its own language.
        payments = [{
            'method': p.method.name,
            'amount': str(p.amount),
            'is_cash': p.method.is_cash,
            'is_store_credit': p.method.is_store_credit,
        } for p in invoice.payments.select_related('method').order_by('created_at')]

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
                'change_given': str(invoice.change_given),
                'salesperson_name': invoice.salesperson_name,   # s171: who sold it
                'source_number': invoice.source_number,         # PROLINE-IMPORT-B.md: the old system's number
            },
            'store': {
                'name': store.name,
                'phone_number': getattr(store, 'phone_number', '') or '',
                'city': getattr(store, 'city', '') or '',
                'country': getattr(store, 'country', '') or '',
                # s167: the receipt prints in the SHOP's language, whoever's screen prints it.
                'default_language': store.default_language or 'en',
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
            'weight_unit': settings_obj.default_weight_unit if settings_obj else 'KG',
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
        from .treasury_flows import post_sale_payment
        with transaction.atomic():
            payment = serializer.save(created_by=self.request.user)
            post_sale_payment(payment)   # TREASURY.md: money taken later on a sale lands in its box too
        invoice = payment.invoice
        log_activity(
            request=self.request,
            action=f"Received Payment {payment.amount} on Sale #{invoice.invoice_number or str(invoice.id)[:8]}",
            op_type=ActivityLog.OperationType.SALE,
            details={
                'payment_id': str(payment.id),
                'invoice_id': str(invoice.id),
                'invoice_number': invoice.invoice_number,
                'customer': invoice.customer.name if invoice.customer_id else None,
                'method': payment.method.name,
                'amount': str(payment.amount),
            },
        )


class CustomerReceiptViewSet(viewsets.ModelViewSet):
    """Receive a customer's debt payment as one lump sum (s167, Proline "دفع عملاء"). Spread oldest-first over
    their unpaid sales, then the opening balance (finance.receipts). Filter the list by ?customer=<uuid>.
    No edit: a wrong receipt is deleted (ADMIN, reverses its sale payments) and entered again.
    MANAGER, like supplier payments: lowering what a customer owes is a credit decision, not a till sale."""
    serializer_class = CustomerReceiptSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':     'MANAGER',
        'retrieve': 'MANAGER',
        'create':   'MANAGER',
        'destroy':  'ADMIN',
    }
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        qs = (CustomerReceipt.objects.filter(store=self.request.user.store)
              .select_related('customer', 'method', 'created_by'))
        customer = self.request.query_params.get('customer')
        if customer:
            qs = qs.filter(customer_id=customer)
        return qs

    def create(self, request, *args, **kwargs):
        from .receipts import receive_customer_payment
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        receipt, summary = receive_customer_payment(
            store=request.user.store, customer=d['customer'], amount=d['amount'], method=d['method'],
            user=request.user, date=d.get('date'), note=d.get('note', ''),
            allow_advance=d.get('allow_advance', False),
        )
        log_activity(
            request=request,
            action=f"Received {receipt.amount} from customer {receipt.customer.name}",
            op_type=ActivityLog.OperationType.SALE,
            details={
                'customer_receipt_id': str(receipt.id),
                'customer': receipt.customer.name,
                'method': receipt.method.name,
                **{k: summary[k] for k in ('to_invoices', 'to_opening_balance', 'advance',
                                           'balance_before', 'balance_after')},
                'amount': str(receipt.amount),
                'invoices': [a['invoice_number'] for a in summary['allocations']],
            },
        )
        data = self.get_serializer(receipt).data
        data['summary'] = summary
        return Response(data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        from .receipts import reverse_customer_receipt
        reverse_customer_receipt(instance, user=self.request.user)
        log_activity(
            request=self.request,
            action=f"Deleted customer payment {instance.amount} from {instance.customer.name}",
            op_type=ActivityLog.OperationType.SALE,
            details={'customer_receipt_id': str(instance.id), 'customer': instance.customer.name,
                     'amount': str(instance.amount)},
        )


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
        'print_data':     'MANAGER',
    }

    def get_queryset(self):
        qs = PurchaseInvoice.objects.filter(
            store=self.request.user.store
        ).prefetch_related('items').select_related('supplier', 'branch')
        p = self.request.query_params
        if q := p.get('q'):
            qs = qs.filter(
                models.Q(vendor_reference__icontains=q) |
                models.Q(supplier__name__icontains=q) |
                models.Q(source_number=q.strip().lstrip('#'))   # an imported purchase's old-system number
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
        # SKU2 on the sticker only while SKU2 is ON and sudo ticked "print on price stickers" (s159).
        from inventory.sku import sku2_active
        st = store.settings
        with_sku2 = sku2_active(store) and st.sku2_print_label
        from inventory.spec_sets import SpecLineBuilder
        spec_lines = SpecLineBuilder()   # Upgrade 9 (s168): same spec line as the product-list stickers
        items = []
        for item in (invoice.items.select_related('variant__product__category__parent__parent__parent')
                     .prefetch_related('variant__attributes')):
            v = item.variant
            items.append({
                'variant_id':   str(v.id),
                'product_name': v.product.name,
                'sku':          v.sku,
                'sku2':         (v.sku2 or '') if with_sku2 else '',
                'barcode':      v.barcode or v.sku,
                'sell_price':   str(v.sell_price),
                'specs':        spec_lines.line(v.product.category, v.attributes.all()),
                'quantity':     int(item.quantity),
            })
        return Response({'store_name': store.name, 'items': items})

    @action(detail=True, methods=['get'], url_path='print-data')
    def print_data(self, request, pk=None):
        """Fully-resolved payload for the printable PURCHASE invoice — store header,
        supplier, purchase #/supplier invoice #, and line items with base cost +
        current retail price. One round-trip so the print view never has to stitch
        lookups together (mirrors the sales-invoice print_data)."""
        invoice = self.get_object()
        store = invoice.store
        settings_obj = getattr(store, 'settings', None)

        currency = None
        if getattr(store, 'currency', None):
            currency = {'symbol': store.currency.symbol, 'position': store.currency.position}

        items = []
        base_total = Decimal('0')
        retail_total = Decimal('0')
        for item in invoice.items.select_related('variant__product', 'unit').all():
            retail = item.variant.sell_price or Decimal('0')
            qty = item.quantity
            items.append({
                'name':       item.variant.product.name,
                'sku':        item.variant.sku,
                'quantity':   str(qty),
                'unit_name':  item.unit.name if item.unit_id else None,  # base unit → None
                'base_cost':  str(item.unit_cost),
                'retail':     str(retail),
                'line_total': str(item.total_cost),
                'serial_numbers': list(item.serial_numbers or []),   # New 7
            })
            base_total += item.total_cost
            retail_total += qty * retail

        return Response({
            'purchase': {
                'purchase_number':  invoice.purchase_number or '',
                'vendor_reference': invoice.vendor_reference or '',
                'status':           invoice.status,
                'date':             invoice.date,
                'notes':            invoice.notes or '',
                'base_total':       str(base_total),
                'retail_total':     str(retail_total),
            },
            'store': {
                'name':         store.name,
                'phone_number': getattr(store, 'phone_number', '') or '',
                'city':         getattr(store, 'city', '') or '',
                'country':      getattr(store, 'country', '') or '',
                'default_language': store.default_language or 'en',   # s167: prints in the shop's language
            },
            'supplier': {'name': invoice.supplier.name} if invoice.supplier else None,
            'branch':   {'name': invoice.branch.name} if invoice.branch else None,
            'legal': {
                'receipt_header': settings_obj.receipt_header if settings_obj else '',
                'receipt_footer': settings_obj.receipt_footer if settings_obj else '',
            },
            'currency': currency,
            'items': items,
        })


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

    # TREASURY.md Upgrade 7: an expense leaves the box it was paid from (EXPENSE_OUT); an edit of amount / box
    # reverses the old line and writes the new one; a delete reverses it. All no-ops with the Treasury off.
    def _sync(self, expense, created=False):
        from .treasury_flows import sync_paid_out
        from treasury.models import MoneyMove
        sync_paid_out(expense, kind=MoneyMove.Kind.EXPENSE_OUT, link='expense', user=self.request.user,
                      created=created, reason_edit='Expense edited', reason_delete='Expense deleted')

    def perform_update(self, serializer):
        with transaction.atomic():
            self._sync(serializer.save())

    def perform_destroy(self, instance):
        with transaction.atomic():
            instance.delete()
            self._sync(instance)

    def perform_create(self, serializer):
        with transaction.atomic():
            expense = serializer.save(store=self.request.user.store)
            self._sync(expense, created=True)
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
        qs = (SupplierPayment.objects.filter(store=self.request.user.store)
              .select_related('created_by', 'payment_method'))
        supplier = self.request.query_params.get('supplier')
        if supplier:
            qs = qs.filter(supplier_id=supplier)
        return qs

    # TREASURY.md: a supplier payment leaves its `paid_from` box (SUPPLIER_OUT); an edit of amount / box reverses the
    # old line and writes the new one; a delete reverses it. All no-ops with the Treasury off.
    def _sync(self, payment, created=False):
        from .treasury_flows import sync_paid_out
        from treasury.models import MoneyMove
        sync_paid_out(payment, kind=MoneyMove.Kind.SUPPLIER_OUT, link='supplier_payment', user=self.request.user,
                      created=created, reason_edit='Supplier payment edited',
                      reason_delete='Supplier payment deleted')

    def perform_update(self, serializer):
        with transaction.atomic():
            self._sync(serializer.save())

    def perform_destroy(self, instance):
        with transaction.atomic():
            instance.delete()
            self._sync(instance)

    def perform_create(self, serializer):
        with transaction.atomic():
            payment = serializer.save(store=self.request.user.store, created_by=self.request.user)
            self._sync(payment, created=True)
        log_activity(
            request=self.request,
            action=f"Recorded Supplier Payment: {payment.amount} → {payment.supplier.name}",
            op_type=ActivityLog.OperationType.EXPENSE,
            details={
                'supplier_payment_id': str(payment.id),
                'supplier': payment.supplier.name,
                'amount': str(payment.amount),
                'method': payment.method,
                'payment_method': payment.payment_method.name if payment.payment_method_id else None,
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
        'drawer':         'CASHIER',
        'drawers':        'CASHIER',   # TREASURY.md: the open-shift screen's drawer picker (book balance + busy)
        'move_targets':   'CASHIER',   # TREASURY.md: the close screen's "move the takings to" picker
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
        already = {'detail': 'You already have an open shift.', 'code': 'shift_already_open'}
        if open_shift:
            raise ValidationError(already)
        from treasury import ledger
        from .treasury_flows import guarded, refuse, _default_drawer
        store = self.request.user.store
        vd = serializer.validated_data
        count_reason = vd.pop('count_reason', '') or ''
        drawer = vd.pop('drawer', None)
        extra = {}
        box_mode = ledger.is_on(store)
        if box_mode:
            # TREASURY.md § 4.2: the shift opens on a DRAWER box of its branch (default = the branch's default
            # drawer). starting_cash = what was counted; ≠ the drawer's book → a COUNT_ADJUST line with the reason.
            from treasury.models import MoneyBox
            branch = vd.get('branch')
            if drawer is None:
                with guarded():
                    drawer = _default_drawer(store, branch)
            if (drawer.store_id != store.id or drawer.kind != MoneyBox.Kind.DRAWER or not drawer.is_active
                    or drawer.branch_id != getattr(branch, 'id', None)):
                refuse('bad_drawer', "Choose one of this branch's till drawers.")
            if (vd.get('starting_cash') or 0) < 0:
                refuse('bad_amount', 'Counted cash cannot be negative.')
            extra ={'drawer': drawer, 'ledger': WorkShift.Ledger.BOX}
        # Two requests at once (double-click) both pass the check above — the DB's one_open_shift_per_user
        # constraint stops the second; answer it with the same clean 400 (2026-10-14).
        from django.db import IntegrityError
        try:
            with transaction.atomic():
                shift = serializer.save(store=store, user=self.request.user, **extra)
                if box_mode:
                    with guarded():
                        ledger.count(drawer, shift.starting_cash, user=self.request.user, reason=count_reason,
                                     shift=shift)
        except IntegrityError as exc:
            if 'one_open_shift_per_drawer' in str(exc):
                refuse('drawer_busy', 'Someone already has an open shift on this drawer.')
            if 'one_open_shift_per_user' not in str(exc):
                raise
            raise ValidationError(already)
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
        # s157: a cashier closes only their OWN drawer; Manager+ may close anyone's.
        if shift.user_id != request.user.id and _user_rank(request.user) < ROLE_RANK[User.Role.MANAGER]:
            return Response({'detail': 'You can only close your own shift.'}, status=status.HTTP_403_FORBIDDEN)
        if shift.status == WorkShift.Status.CLOSED:
            return Response({'detail': 'Shift already closed.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            counted_cash = Decimal(str(request.data.get('counted_cash', 0) or 0))
        except (ArithmeticError, ValueError):
            return Response({'counted_cash': 'Enter the cash you counted as a number.'}, status=status.HTTP_400_BAD_REQUEST)
        if counted_cash < 0:
            return Response({'counted_cash': 'Counted cash cannot be negative.'}, status=status.HTTP_400_BAD_REQUEST)
        from treasury import ledger
        if shift.ledger == WorkShift.Ledger.BOX and shift.drawer_id and ledger.is_on(shift.store):
            shift = self._close_on_box(request, shift, counted_cash)
        else:
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
                i18n=msg(f'shift_{direction}.title', 'shift_diff.body',   # s167
                         cashier=shift.user.get_full_name() or shift.user.username,
                         diff=f"{shift.difference:+.2f}"),
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

    def _close_on_box(self, request, shift, counted_cash):
        """TREASURY.md § 4.2 — close a shift that runs on a DRAWER box: expected = the drawer's book balance;
        counted ≠ expected → a COUNT_ADJUST line (`count_reason` required); then optionally `move_to_box` +
        `move_amount` = a transfer from the drawer (e.g. the takings to the safe). All or nothing."""
        from treasury import ledger
        from treasury.models import MoneyBox
        from .treasury_flows import guarded, refuse, check_move_target
        reason = str(request.data.get('count_reason') or '')
        move_to, move_amount = request.data.get('move_to_box'), request.data.get('move_amount')
        target = None
        if move_to not in (None, ''):
            try:
                target = MoneyBox.all_objects.filter(pk=move_to, store=shift.store).first()
            except (ValueError, TypeError, DjangoValidationError):
                target = None
            # Same shop, open, never a drawer / a box with an open shift; below MANAGER only cash-like boxes.
            check_move_target(target, shift.store, request.user)
            try:
                move_amount = Decimal(str(move_amount))
            except (ArithmeticError, ValueError, TypeError):
                refuse('bad_amount', 'Enter the amount to move as a number.')
        with transaction.atomic():
            shift = WorkShift.all_objects.select_for_update().get(pk=shift.pk)
            if shift.status == WorkShift.Status.CLOSED:
                refuse('shift_closed', 'Shift already closed.')
            drawer = MoneyBox.all_objects.select_for_update().get(pk=shift.drawer_id)
            with guarded():
                expected = ledger.balance(drawer)
                ledger.count(drawer, counted_cash, user=request.user, reason=reason, shift=shift)
                shift.expected_cash = expected
                shift.closing_cash = counted_cash
                shift.difference = counted_cash - expected
                shift.end_time = tz.now()
                shift.status = WorkShift.Status.CLOSED
                shift.save()
                if target is not None:
                    ledger.transfer(drawer, target, move_amount, user=request.user)
        return shift

    @action(detail=False, methods=['get'], url_path='move-targets')
    def move_targets(self, request):
        """TREASURY.md § 4.2: the boxes the CALLER may move the takings to when closing — exactly the close rule
        (same shop, open, never a drawer / a box with an open shift; below MANAGER only cash-like boxes).
        `[{id, name, kind}]` (+ `balance` for MANAGER+)."""
        from treasury import ledger
        from .treasury_flows import move_targets
        store = request.user.store
        if not ledger.is_on(store):
            return Response({'code': 'treasury_off', 'detail': 'The Treasury is off for this shop.'},
                            status=status.HTTP_403_FORBIDDEN)
        return Response(move_targets(store, request.user))

    @action(detail=False, methods=['get'])
    def drawers(self, request):
        """TREASURY.md § 4.2: the shop's till drawers (?branch=<id>) for the open-shift screen — each with its book
        balance (what the cashier should find) and whether someone already has an open shift on it."""
        from treasury import ledger
        store = request.user.store
        if not ledger.is_on(store):
            return Response({'code': 'treasury_off', 'detail': 'The Treasury is off for this shop.'},
                            status=status.HTTP_403_FORBIDDEN)
        from .treasury_flows import drawers_with_balance, is_manager
        manager = is_manager(request.user)
        branch = None
        if not manager and not request.query_params.get('branch'):
            # Below MANAGER: one branch at a time (the one being opened in), never the whole shop's drawers.
            return Response({'code': 'branch_required', 'detail': 'Choose the branch.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if request.query_params.get('branch'):
            try:
                branch = Branch.objects.filter(store=store, pk=request.query_params['branch']).first()
            except (ValueError, TypeError, DjangoValidationError):
                branch = None
            if branch is None:
                return Response({'code': 'branch_not_found', 'detail': 'Branch not found.'},
                                status=status.HTTP_400_BAD_REQUEST)
        from treasury.models import MoneyBox
        branches = [branch] if branch is not None else list(Branch.objects.filter(store=store, is_deleted=False))
        if any(not MoneyBox.all_objects.filter(branch=b, is_default_drawer=True).exists() for b in branches):
            ledger.setup(store)   # a branch added after the switch was turned on gets its drawer
        return Response(drawers_with_balance(store, branch, show_who=manager))

    @action(detail=False, methods=['get'])
    def drawer(self, request):
        """s157 (§AUDIT A10): the caller's open drawer — cash in (cash payments they took), cash out (cash
        refunds paid from this drawer), expected balance, and each move. Same rules as closing the shift.
        TREASURY.md: a shift on a DRAWER box answers from the box's lines since the shift opened (same shape)."""
        shift = (WorkShift.objects.filter(store=request.user.store, user=request.user,
                                          status=WorkShift.Status.OPEN).first())
        if shift is None:
            return Response({'shift': None, 'cash_in': '0', 'cash_out': '0', 'expected_balance': '0', 'moves': []})
        from treasury import ledger
        if shift.ledger == WorkShift.Ledger.BOX and shift.drawer_id and ledger.is_on(shift.store):
            return Response(self._box_drawer(shift))
        pays = (Payment.objects.filter(invoice__store=shift.store, created_at__gte=shift.start_time,
                                       method__is_cash=True, created_by=shift.user, deposit_of__isnull=True)
                .values('created_at', 'amount', 'invoice__invoice_number'))
        refunds = (RefundInvoice.objects.filter(shift=shift, drawer_cash__gt=0)
                   .values('created_at', 'drawer_cash', 'refund_number', 'original_invoice__invoice_number'))
        moves = [{'time': p['created_at'], 'kind': 'IN', 'invoice_number': p['invoice__invoice_number'],
                  'refund_number': None, 'amount': str(p['amount'])} for p in pays]
        moves += [{'time': r['created_at'], 'kind': 'OUT', 'invoice_number': r['original_invoice__invoice_number'],
                   'refund_number': r['refund_number'], 'amount': str(r['drawer_cash'])} for r in refunds]
        # s167: the part of a customer's lump-sum payment that paid no sale (its sale part is in `pays` above)
        received = (CustomerReceipt.objects.filter(store=shift.store, created_at__gte=shift.start_time,
                                                   method__is_cash=True, created_by=shift.user, unallocated__gt=0)
                    .values('created_at', 'unallocated', 'customer__name'))
        moves += [{'time': c['created_at'], 'kind': 'IN', 'invoice_number': None, 'refund_number': None,
                   'customer': c['customer__name'], 'amount': str(c['unallocated'])} for c in received]
        # s172: cash reservation deposits taken (IN) / given back (OUT) in this shift
        from .models import ReservationDeposit
        deps = (ReservationDeposit.objects.filter(shift=shift, method__is_cash=True)
                .values('created_at', 'amount', 'reservation__number', 'reservation__customer__name'))
        moves += [{'time': d['created_at'], 'kind': 'IN' if d['amount'] > 0 else 'OUT', 'invoice_number': None,
                   'refund_number': None, 'reservation_number': d['reservation__number'],
                   'customer': d['reservation__customer__name'], 'amount': str(abs(d['amount']))} for d in deps]
        moves.sort(key=lambda m: m['time'], reverse=True)
        cash_in, cash_out = shift.cash_in(), shift.cash_out()
        return Response({
            'shift': WorkShiftSerializer(shift).data,
            'cash_in': str(cash_in), 'cash_out': str(cash_out),
            'expected_balance': str(shift.starting_cash + cash_in - cash_out),
            'moves': moves,
        })

    @staticmethod
    def _box_drawer(shift):
        """The `drawer` answer for a shift on a DRAWER box: every line on the drawer since the shift opened (sales,
        receipts, refunds, cash expenses, supplier cash, transfers, counts) — same keys as the old answer, plus
        `line_kind` (the line's kind) and `note`. expected_balance = the drawer's book balance."""
        from treasury import ledger
        from treasury.serializers import owner_level_type
        from .treasury_flows import drawer_lines
        moves, cash_in, cash_out = [], Decimal('0'), Decimal('0')
        for m in drawer_lines(shift):
            if m.amount > 0:
                cash_in += m.amount
            else:
                cash_out -= m.amount
            refund = m.refund
            moves.append({
                'time': m.at,
                'kind': 'IN' if m.amount > 0 else 'OUT',
                'line_kind': m.kind,
                'invoice_number': (m.payment.invoice.invoice_number if m.payment_id else
                                   (refund.original_invoice.invoice_number
                                    if refund is not None and refund.original_invoice_id else None)),
                'refund_number': refund.refund_number if refund is not None else None,
                'customer': m.customer_receipt.customer.name if m.customer_receipt_id else None,
                'amount': str(abs(m.amount)),
                # An owner draw / loan / partner line: the owner's own words stay private (TREASURY.md § 12.4).
                'note': '' if owner_level_type(m) else (m.note or m.reason or ''),
            })
        return {
            'shift': WorkShiftSerializer(shift).data,
            'cash_in': str(cash_in), 'cash_out': str(cash_out),
            'expected_balance': str(ledger.balance(shift.drawer)),
            'moves': moves,
        }

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

    def perform_destroy(self, instance):
        # TREASURY.md: a deleted return's payout comes back into the boxes it left (no-op with the Treasury off).
        from .treasury_flows import reverse_refund
        with transaction.atomic():
            instance.delete()
            reverse_refund(instance, self.request.user)

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


def _is_uuid(value):
    import uuid
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class PurchaseReturnViewSet(viewsets.ReadOnlyModelViewSet):
    """Return goods to a supplier (s171). POST = create AND post (stock out, account credit, money back in a box) —
    all in finance/purchase_returns.py. No edit, no delete: a mistake is voided (ADMIN), which undoes everything.

    GET ?supplier= &purchase= &status= · GET returnable/?purchase=<id> = that purchase's lines with what can still
    go back."""
    serializer_class = PurchaseReturnSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':       'MANAGER',
        'retrieve':   'MANAGER',
        'create':     'MANAGER',
        'returnable': 'MANAGER',
        'void':       'ADMIN',
    }

    def get_queryset(self):
        qs = (PurchaseReturn.objects.filter(store=self.request.user.store)
              .select_related('supplier', 'branch', 'purchase', 'refund_method', 'refund_box', 'created_by')
              .prefetch_related('items__variant__product', 'items__unit'))
        p = self.request.query_params
        for key in ('supplier', 'purchase'):
            if p.get(key):
                if not _is_uuid(p[key]):
                    return qs.none()
                qs = qs.filter(**{key + '_id': p[key]})
        if p.get('status'):
            qs = qs.filter(status=p['status'])
        return qs

    def get_object(self):
        if not _is_uuid(self.kwargs.get('pk')):
            from django.http import Http404
            raise Http404
        return super().get_object()

    def create(self, request, *args, **kwargs):
        from inventory.models import Supplier
        from treasury.models import MoneyBox
        from .purchase_returns import create_return
        store = request.user.store
        d = request.data

        def pick(model, key, **scope):
            val = d.get(key)
            if not val:
                return None
            if not _is_uuid(val):
                raise ValidationError({key: 'Not found.'})
            obj = model.all_objects.filter(pk=val, **scope).first() if hasattr(model, 'all_objects') \
                else model.objects.filter(pk=val, **scope).first()
            if obj is None:
                raise ValidationError({key: 'Not found.'})
            return obj

        supplier = pick(Supplier, 'supplier', store=store)
        purchase = pick(PurchaseInvoice, 'purchase', store=store)
        if supplier is None and purchase is not None:
            supplier = purchase.supplier
        branch = pick(Branch, 'branch', store=store)
        method = pick(PaymentMethod, 'refund_method', store=store)
        box = pick(MoneyBox, 'refund_box', store=store)
        lines = d.get('lines') or []
        if not isinstance(lines, list):
            raise ValidationError({'lines': 'Send a list of lines.'})
        ret = create_return(store, request.user, supplier=supplier, branch=branch, purchase=purchase, lines=lines,
                            refund_amount=d.get('refund_amount') or 0, refund_method=method, refund_box=box,
                            notes=d.get('notes') or '')
        log_activity(
            request=request,
            action=f"Returned to supplier #{ret.return_number}: {ret.total_amount} → {ret.supplier.name}",
            op_type=ActivityLog.OperationType.PURCHASE,
            details={'purchase_return_id': str(ret.id), 'supplier': ret.supplier.name,
                     'total': str(ret.total_amount), 'refund': str(ret.refund_amount)},
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=ret.pk)).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def void(self, request, pk=None):
        from .purchase_returns import void_return
        ret = void_return(self.get_object(), request.user, request.data.get('reason') or '')
        log_activity(
            request=request,
            action=f"Voided supplier return #{ret.return_number} ({ret.supplier.name})",
            op_type=ActivityLog.OperationType.PURCHASE,
            details={'purchase_return_id': str(ret.id), 'reason': ret.void_reason},
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=ret.pk)).data)

    @action(detail=False, methods=['get'])
    def returnable(self, request):
        from .purchase_returns import returned_qty
        pid = request.query_params.get('purchase')
        if not _is_uuid(pid):
            return Response({'purchase': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        purchase = (PurchaseInvoice.objects.filter(store=request.user.store, pk=pid)
                    .select_related('supplier', 'branch').first())
        if purchase is None:
            return Response({'purchase': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        from django.db.models import Q
        from inventory.models import SerialUnit
        lines = []
        for it in purchase.items.select_related('variant__product', 'unit').order_by('id'):
            back = returned_qty(it)
            # Serial-tracked: the units that can really go back now — on the shelf at the purchase's branch, brought
            # in by THIS line or registered later with no purchase line (inventory.serials.supplier_return).
            on_shelf = list(SerialUnit.objects.filter(
                store=request.user.store, variant=it.variant, branch=purchase.branch,
                status=SerialUnit.Status.IN_STOCK).filter(Q(purchase_item=it) | Q(purchase_item__isnull=True))
                .order_by('serial').values_list('serial', flat=True)) if it.variant.product.track_serials else []
            lines.append({
                'purchase_item': it.id, 'variant': str(it.variant_id), 'product_name': it.variant.product.name,
                'sku': it.variant.sku, 'unit': str(it.unit_id) if it.unit_id else None,
                'unit_name': it.unit.name if it.unit_id else '', 'unit_factor': str(it.unit_factor),
                'bought': str(it.quantity), 'returned': str(back), 'left': str(Decimal(str(it.quantity)) - back),
                'unit_cost': str(it.unit_cost), 'track_serials': bool(it.variant.product.track_serials),
                'serial_numbers': on_shelf,
            })
        return Response({'purchase': str(purchase.id), 'status': purchase.status,
                         'supplier': str(purchase.supplier_id) if purchase.supplier_id else None,
                         'supplier_name': purchase.supplier.name if purchase.supplier_id else '',
                         'branch': str(purchase.branch_id), 'lines': lines})


class ChequeViewSet(viewsets.ReadOnlyModelViewSet):
    """Cheques received / given (s171). GET ?status= &direction= &due=soon|overdue &customer= &supplier=
    POST = a new waiting cheque · POST {id}/clear/ {method?, box?, on?} · {id}/bounce/ {reason} · {id}/cancel/ {reason}.
    All state changes in finance/cheques.py. A bounce AFTER clearing reverses a payment → ADMIN."""
    serializer_class = ChequeSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list': 'MANAGER', 'retrieve': 'MANAGER', 'create': 'MANAGER',
        'clear': 'MANAGER', 'bounce': 'MANAGER', 'cancel': 'MANAGER', 'summary': 'MANAGER',
    }
    SOON_DAYS = 7

    def get_queryset(self):
        qs = (Cheque.objects.filter(store=self.request.user.store)
              .select_related('customer', 'supplier', 'method', 'created_by'))
        p = self.request.query_params
        if p.get('status'):
            qs = qs.filter(status=p['status'])
        if p.get('direction'):
            qs = qs.filter(direction=p['direction'])
        for key in ('customer', 'supplier'):
            if p.get(key):
                if not _is_uuid(p[key]):
                    return qs.none()
                qs = qs.filter(**{key + '_id': p[key]})
        today = tz.localdate()
        if p.get('due') == 'soon':
            qs = qs.filter(status=Cheque.Status.PENDING, due_date__gte=today,
                           due_date__lte=today + timedelta(days=self.SOON_DAYS))
        elif p.get('due') == 'overdue':
            qs = qs.filter(status=Cheque.Status.PENDING, due_date__lt=today)
        return qs

    def get_object(self):
        if not _is_uuid(self.kwargs.get('pk')):
            from django.http import Http404
            raise Http404
        return super().get_object()

    def _pick(self, model, key, data=None, **scope):
        val = (data if data is not None else self.request.data).get(key)
        if not val:
            return None
        obj = model.all_objects.filter(pk=val, **scope).first() if _is_uuid(val) else None
        if obj is None:
            raise ValidationError({key: 'Not found.'})
        return obj

    def _date(self, key):
        from django.utils.dateparse import parse_date
        raw = (self.request.data.get(key) or '').strip() if isinstance(self.request.data.get(key), str) else ''
        if not raw:
            return None
        try:
            d = parse_date(raw)
        except ValueError:
            d = None
        if d is None:
            raise ValidationError({key: 'Use a date like 2026-01-31.'})
        return d

    def create(self, request, *args, **kwargs):
        from inventory.models import Supplier
        from users.models import Customer
        from .cheques import create_cheque
        store = request.user.store
        ch = create_cheque(
            store, request.user, direction=request.data.get('direction'),
            customer=self._pick(Customer, 'customer', store=store),
            supplier=self._pick(Supplier, 'supplier', store=store),
            number=request.data.get('number') or '', bank_name=request.data.get('bank_name') or '',
            amount=request.data.get('amount'), issue_date=self._date('issue_date'), due_date=self._date('due_date'),
            method=self._pick(PaymentMethod, 'method', store=store), note=request.data.get('note') or '')
        log_activity(request=request, action=f"Recorded cheque {ch.number}: {ch.amount} ({ch.party_name})",
                     op_type=ActivityLog.OperationType.OTHER, details={'cheque_id': str(ch.id)})
        return Response(self.get_serializer(ch).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def clear(self, request, pk=None):
        from treasury.models import MoneyBox
        from .cheques import clear_cheque
        store = request.user.store
        ch = clear_cheque(self.get_object(), request.user,
                          method=self._pick(PaymentMethod, 'method', store=store),
                          box=self._pick(MoneyBox, 'box', store=store), on=self._date('on'))
        log_activity(request=request, action=f"Cheque {ch.number} cleared: {ch.amount} ({ch.party_name})",
                     op_type=ActivityLog.OperationType.OTHER, details={'cheque_id': str(ch.id)})
        return Response(self.get_serializer(ch).data)

    @action(detail=True, methods=['post'])
    def bounce(self, request, pk=None):
        from users.permissions import ROLE_RANK, _user_rank
        from .cheques import bounce_cheque
        ch = self.get_object()
        if ch.status == Cheque.Status.CLEARED and _user_rank(request.user) < ROLE_RANK['ADMIN']:
            raise PermissionDenied('Only an admin can bounce a cheque that was already cleared (it reverses a payment).')
        ch = bounce_cheque(ch, request.user, request.data.get('reason') or '')
        log_activity(request=request, action=f"Cheque {ch.number} bounced ({ch.party_name})",
                     op_type=ActivityLog.OperationType.OTHER, details={'cheque_id': str(ch.id), 'reason': ch.status_reason})
        return Response(self.get_serializer(ch).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        from .cheques import cancel_cheque
        ch = cancel_cheque(self.get_object(), request.user, request.data.get('reason') or '')
        log_activity(request=request, action=f"Cheque {ch.number} cancelled ({ch.party_name})",
                     op_type=ActivityLog.OperationType.OTHER, details={'cheque_id': str(ch.id), 'reason': ch.status_reason})
        return Response(self.get_serializer(ch).data)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """The reminder: how many waiting cheques are overdue / due in the next 7 days, and their totals."""
        from django.db.models import Count, Sum
        today = tz.localdate()
        base = Cheque.objects.filter(store=request.user.store, status=Cheque.Status.PENDING)
        out = {}
        for key, qs in (('overdue', base.filter(due_date__lt=today)),
                        ('soon', base.filter(due_date__gte=today, due_date__lte=today + timedelta(days=self.SOON_DAYS)))):
            for d in (Cheque.Direction.RECEIVED, Cheque.Direction.GIVEN):
                agg = qs.filter(direction=d).aggregate(n=Count('id'), total=Sum('amount'))
                out[f'{key}_{d.lower()}'] = {'count': agg['n'] or 0, 'total': str(agg['total'] or Decimal('0'))}
        return Response(out)
