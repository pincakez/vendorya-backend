from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.core.management import call_command

from users.permissions import IsSuperAdmin, IsOwner
from .models import SubscriptionPlan, Subscription, BillingInvoice, BillingSettings
from .serializers import (
    SubscriptionPlanSerializer,
    AdminSubscriptionSerializer,
    TenantSubscriptionSerializer,
    BillingInvoiceSerializer,
    AdminBillingInvoiceCreateSerializer,
    BillingSettingsSerializer,
)


# ---------- Sudo: platform billing settings (singleton) ----------

class AdminBillingSettingsView(APIView):
    """GET / PATCH the single platform-wide BillingSettings row. Sudo only.

    `POST run-cycle` triggers `run_billing_cycle` on demand (the "Run now"
    button on the settings page).
    """
    permission_classes = [IsSuperAdmin]

    def get(self, request):
        return Response(BillingSettingsSerializer(BillingSettings.load()).data)

    def patch(self, request):
        obj = BillingSettings.load()
        ser = BillingSettingsSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)


class AdminBillingRunCycleView(APIView):
    """Run the billing cycle on demand. Sudo only."""
    permission_classes = [IsSuperAdmin]

    def post(self, request):
        call_command('run_billing_cycle', force=True)
        return Response(BillingSettingsSerializer(BillingSettings.load()).data)


# ---------- Sudo: plans ----------

class AdminSubscriptionPlanViewSet(viewsets.ModelViewSet):
    """CRUD over plans. Sudo only."""
    serializer_class   = SubscriptionPlanSerializer
    permission_classes = [IsSuperAdmin]
    queryset           = SubscriptionPlan.objects.filter(is_deleted=False)
    filter_backends    = [filters.SearchFilter]
    search_fields      = ['name', 'description']


# ---------- Sudo: subscriptions ----------

class AdminSubscriptionViewSet(viewsets.ModelViewSet):
    """List / change every store's subscription. Sudo only."""
    serializer_class   = AdminSubscriptionSerializer
    permission_classes = [IsSuperAdmin]
    filter_backends    = [filters.SearchFilter]
    search_fields      = ['store__name', 'store__owner__username', 'custom_label', 'plan__name']
    http_method_names  = ['get', 'patch', 'head', 'options']

    def get_queryset(self):
        qs = (Subscription.all_objects   # sudo: every store's subscription
              .select_related('store', 'store__owner', 'plan')
              .filter(store__is_deleted=False))
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        plan_id = self.request.query_params.get('plan')
        if plan_id:
            qs = qs.filter(plan_id=plan_id)
        return qs


# ---------- Sudo: invoices ----------

class AdminBillingInvoiceViewSet(viewsets.ModelViewSet):
    """Issue + manage tenant billing invoices. Sudo only."""
    permission_classes = [IsSuperAdmin]
    filter_backends    = [filters.SearchFilter]
    search_fields      = ['invoice_number', 'store__name', 'line_description']
    http_method_names  = ['get', 'post', 'patch', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'create':
            return AdminBillingInvoiceCreateSerializer
        return BillingInvoiceSerializer

    def get_queryset(self):
        qs = (BillingInvoice.all_objects   # sudo: every store's invoices
              .select_related('store', 'subscription', 'subscription__plan'))
        params = self.request.query_params
        if params.get('store'):
            qs = qs.filter(store_id=params['store'])
        if params.get('status'):
            qs = qs.filter(status=params['status'])
        return qs

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data, context={'request': request})
        ser.is_valid(raise_exception=True)
        invoice = ser.save()
        return Response(BillingInvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def issue(self, request, pk=None):
        invoice = self.get_object()
        invoice.issue(by_user=request.user)
        return Response(BillingInvoiceSerializer(invoice).data)

    @action(detail=True, methods=['post'], url_path='mark-paid')
    def mark_paid(self, request, pk=None):
        invoice = self.get_object()
        method    = request.data.get('method', '')
        reference = request.data.get('reference', '')
        invoice.mark_paid(method=method, reference=reference)
        # Drop a "payment received" notification in the tenant inbox.
        from notifications.models import Notification
        if invoice.status == BillingInvoice.Status.PAID:
            Notification.objects.create(
                store=invoice.store,
                user=invoice.store.owner,
                type=Notification.Type.BILLING_PAID,
                title=f"Payment received for {invoice.invoice_number}",
                body=f"Thank you — {invoice.amount} {invoice.currency} received.",
                link=f"/settings/billing/invoices/{invoice.id}",
                payload={'invoice_id': str(invoice.id), 'invoice_number': invoice.invoice_number},
            )
        return Response(BillingInvoiceSerializer(invoice).data)

    @action(detail=True, methods=['post'])
    def void(self, request, pk=None):
        invoice = self.get_object()
        if invoice.status == BillingInvoice.Status.PAID:
            return Response({'detail': 'Cannot void a paid invoice.'},
                            status=status.HTTP_400_BAD_REQUEST)
        invoice.status = BillingInvoice.Status.VOID
        invoice.save()
        return Response(BillingInvoiceSerializer(invoice).data)


# ---------- Tenant: read-only view of own subscription + invoices ----------

class TenantSubscriptionView(APIView):
    """The store sees its own current subscription. OWNER only."""
    permission_classes = [IsAuthenticated, IsOwner]

    def get(self, request):
        store = request.user.store
        if not store:
            return Response({'detail': 'User has no store assigned.'}, status=403)
        try:
            sub = Subscription.objects.select_related('plan').get(store=store)
        except Subscription.DoesNotExist:
            return Response({'detail': 'No subscription found.'}, status=404)
        from .quota import quota_status
        data = TenantSubscriptionSerializer(sub).data
        data['quota'] = quota_status(store)
        return Response(data)


class TenantBillingInvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    """Tenant's own billing invoices. OWNER only — financial data."""
    serializer_class   = BillingInvoiceSerializer
    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
        if not self.request.user.store:
            return BillingInvoice.objects.none()
        return (BillingInvoice.objects
                .filter(store=self.request.user.store)
                .exclude(status=BillingInvoice.Status.DRAFT)
                .select_related('subscription', 'subscription__plan'))
