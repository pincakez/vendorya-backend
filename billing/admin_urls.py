from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    AdminSubscriptionPlanViewSet,
    AdminSubscriptionViewSet,
    AdminBillingInvoiceViewSet,
    AdminBillingSettingsView,
    AdminBillingRunCycleView,
)


router = DefaultRouter()
router.register(r'plans',         AdminSubscriptionPlanViewSet, basename='admin-plan')
router.register(r'subscriptions', AdminSubscriptionViewSet,     basename='admin-subscription')
router.register(r'invoices',      AdminBillingInvoiceViewSet,   basename='admin-billing-invoice')

urlpatterns = [
    path('settings/',         AdminBillingSettingsView.as_view(), name='admin-billing-settings'),
    path('settings/run-cycle/', AdminBillingRunCycleView.as_view(), name='admin-billing-run-cycle'),
    path('', include(router.urls)),
]
