from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import TenantSubscriptionView, TenantBillingInvoiceViewSet


router = DefaultRouter()
router.register(r'invoices', TenantBillingInvoiceViewSet, basename='billing-invoice')

urlpatterns = [
    path('subscription/', TenantSubscriptionView.as_view(), name='billing-subscription'),
    path('', include(router.urls)),
]
