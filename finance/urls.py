from rest_framework.routers import DefaultRouter
from .payment_due_views import PaymentDueViewSet
from .reservation_views import ReservationViewSet
from .views import (
    PaymentMethodViewSet, SalesInvoiceViewSet, PaymentViewSet, CustomerReceiptViewSet,
    PurchaseInvoiceViewSet, SupplierPaymentViewSet,
    ExpenseCategoryViewSet, ExpenseViewSet,
    WorkShiftViewSet, RefundInvoiceViewSet, PurchaseReturnViewSet, ChequeViewSet,
)

router = DefaultRouter()
router.register('payment-methods', PaymentMethodViewSet, basename='payment-methods')
router.register('invoices', SalesInvoiceViewSet, basename='invoices')
router.register('payments', PaymentViewSet, basename='payments')
router.register('customer-receipts', CustomerReceiptViewSet, basename='customer-receipts')
router.register('purchases', PurchaseInvoiceViewSet, basename='purchases')
router.register('supplier-payments', SupplierPaymentViewSet, basename='supplier-payments')
router.register('expense-categories', ExpenseCategoryViewSet, basename='expense-categories')
router.register('expenses', ExpenseViewSet, basename='expenses')
router.register('shifts', WorkShiftViewSet, basename='shifts')
router.register('refunds', RefundInvoiceViewSet, basename='refunds')
router.register('purchase-returns', PurchaseReturnViewSet, basename='purchase-returns')
router.register('cheques', ChequeViewSet, basename='cheques')
router.register('reservations', ReservationViewSet, basename='reservations')
router.register('payment-dues', PaymentDueViewSet, basename='payment-dues')

urlpatterns = router.urls
