from rest_framework.routers import DefaultRouter
from .views import (
    PaymentMethodViewSet, SalesInvoiceViewSet, PaymentViewSet,
    PurchaseInvoiceViewSet, ExpenseCategoryViewSet, ExpenseViewSet,
    WorkShiftViewSet, RefundInvoiceViewSet,
)

router = DefaultRouter()
router.register('payment-methods', PaymentMethodViewSet, basename='payment-methods')
router.register('invoices', SalesInvoiceViewSet, basename='invoices')
router.register('payments', PaymentViewSet, basename='payments')
router.register('purchases', PurchaseInvoiceViewSet, basename='purchases')
router.register('expense-categories', ExpenseCategoryViewSet, basename='expense-categories')
router.register('expenses', ExpenseViewSet, basename='expenses')
router.register('shifts', WorkShiftViewSet, basename='shifts')
router.register('refunds', RefundInvoiceViewSet, basename='refunds')

urlpatterns = router.urls
