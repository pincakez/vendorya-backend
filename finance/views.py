from rest_framework import viewsets, permissions, filters, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from .models import (
    SalesInvoice, Payment, PaymentMethod,
    PurchaseInvoice,
    Expense, ExpenseCategory,
    WorkShift,
    RefundInvoice,
)
from .serializers import (
    SalesInvoiceSerializer, PaymentSerializer, PaymentMethodSerializer,
    PurchaseInvoiceSerializer,
    ExpenseSerializer, ExpenseCategorySerializer,
    WorkShiftSerializer,
    RefundInvoiceSerializer,
)
from core.activity import log_activity
from core.models import ActivityLog


class PaymentMethodViewSet(viewsets.ModelViewSet):
    serializer_class = PaymentMethodSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return PaymentMethod.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class SalesInvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = SalesInvoiceSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['date', 'created_at', 'grand_total', 'invoice_number']

    def get_queryset(self):
        return SalesInvoice.objects.filter(
            store=self.request.user.store
        ).prefetch_related('items', 'payments').select_related('customer', 'branch')

    def perform_create(self, serializer):
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


class PaymentViewSet(viewsets.ModelViewSet):
    serializer_class = PaymentSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        return Payment.objects.filter(invoice__store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class PurchaseInvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = PurchaseInvoiceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return PurchaseInvoice.objects.filter(
            store=self.request.user.store
        ).prefetch_related('items').select_related('supplier', 'branch')

    def perform_create(self, serializer):
        purchase = serializer.save(store=self.request.user.store)
        log_activity(
            request=self.request,
            action=f"Created Purchase #{purchase.invoice_number}",
            op_type=ActivityLog.OperationType.PURCHASE,
            details={
                'purchase_id': str(purchase.id),
                'invoice_number': purchase.invoice_number,
                'supplier': purchase.supplier.name if purchase.supplier else None,
                'grand_total': str(purchase.grand_total),
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
        invoice.status = PurchaseInvoice.Status.RECEIVED
        invoice.save()
        log_activity(
            request=request,
            action=f"Received Purchase #{invoice.invoice_number}",
            op_type=ActivityLog.OperationType.PURCHASE,
            details={
                'purchase_id': str(invoice.id),
                'invoice_number': invoice.invoice_number,
                'supplier': invoice.supplier.name if invoice.supplier else None,
            },
        )
        return Response(PurchaseInvoiceSerializer(invoice).data)


class ExpenseCategoryViewSet(viewsets.ModelViewSet):
    serializer_class = ExpenseCategorySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ExpenseCategory.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class ExpenseViewSet(viewsets.ModelViewSet):
    serializer_class = ExpenseSerializer
    permission_classes = [permissions.IsAuthenticated]
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


class WorkShiftViewSet(viewsets.ModelViewSet):
    serializer_class = WorkShiftSerializer
    permission_classes = [permissions.IsAuthenticated]

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


class RefundInvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = RefundInvoiceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return RefundInvoice.objects.filter(
            store=self.request.user.store
        ).prefetch_related('items').select_related('customer', 'branch', 'original_invoice')

    def perform_create(self, serializer):
        refund = serializer.save(store=self.request.user.store)
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
