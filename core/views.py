from django.utils import timezone
from django.db.models import Sum, Count
from rest_framework import viewsets, status, filters
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from users.permissions import (
    RoleScopedPermission, IsCashierOrAbove, IsManagerOrAbove, IsOwner,
    IsSuperAdmin,
)
from .models import Branch, ActivityLog, Currency, LabelPreset
from .serializers import (
    StoreSerializer, BranchSerializer, StoreSettingsSerializer,
    ActivityLogSerializer, CurrencySerializer, LabelPresetSerializer,
)
from users.models import User

_NO_STORE = Response({'detail': 'User has no store assigned.'}, status=status.HTTP_403_FORBIDDEN)


class StoreView(APIView):
    """GET = any active staff (the sidebar needs the store name).
    PATCH = OWNER only (or super-admin acting as store)."""

    def get_permissions(self):
        if self.request.method == 'PATCH':
            return [IsAuthenticated(), IsOwner()]
        return [IsAuthenticated(), IsCashierOrAbove()]

    def get(self, request):
        if not request.user.store:
            return _NO_STORE
        return Response(StoreSerializer(request.user.store).data)

    def patch(self, request):
        if not request.user.store:
            return _NO_STORE
        serializer = StoreSerializer(request.user.store, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class StoreLogoView(APIView):
    """PATCH /api/core/store/logo/ — multipart upload for logo_light and/or logo_dark.
    Owner only. Accepts fields: logo_light (file), logo_dark (file),
    clear_logo_light (bool), clear_logo_dark (bool)."""
    permission_classes = [IsAuthenticated, IsOwner]

    def patch(self, request):
        if not request.user.store:
            return _NO_STORE
        store = request.user.store
        if request.data.get('clear_logo_light') in (True, 'true', '1'):
            store.logo_light.delete(save=False)
            store.logo_light = None
        if request.data.get('clear_logo_dark') in (True, 'true', '1'):
            store.logo_dark.delete(save=False)
            store.logo_dark = None
        if 'logo_light' in request.FILES:
            store.logo_light = request.FILES['logo_light']
        if 'logo_dark' in request.FILES:
            store.logo_dark = request.FILES['logo_dark']
        store.save(update_fields=['logo_light', 'logo_dark'])
        serializer = StoreSerializer(store, context={'request': request})
        return Response({'logo_light_url': serializer.data['logo_light_url'],
                         'logo_dark_url':  serializer.data['logo_dark_url']})


class StoreSettingsView(APIView):
    """GET = manager+, PATCH = owner only."""

    def get_permissions(self):
        if self.request.method == 'PATCH':
            return [IsAuthenticated(), IsOwner()]
        return [IsAuthenticated(), IsManagerOrAbove()]

    def get(self, request):
        if not request.user.store:
            return _NO_STORE
        return Response(StoreSettingsSerializer(request.user.store.settings).data)

    def patch(self, request):
        if not request.user.store:
            return _NO_STORE
        serializer = StoreSettingsSerializer(request.user.store.settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class CurrencyViewSet(viewsets.ModelViewSet):
    """Currency master list.  Read-only for everyone authenticated, mutations
    restricted to super-admins.  Order: code asc."""
    serializer_class = CurrencySerializer
    queryset = Currency.objects.filter(is_deleted=False).order_by('code')
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), IsCashierOrAbove()]
        return [IsAuthenticated(), IsSuperAdmin()]


class BranchViewSet(viewsets.ModelViewSet):
    serializer_class = BranchSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'ADMIN',
        'update':         'ADMIN',
        'partial_update': 'ADMIN',
    }
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        return Branch.objects.filter(store=self.request.user.store).select_related('address')

    def perform_create(self, serializer):
        from billing.quota import enforce_quota
        enforce_quota(self.request.user.store, 'branches')
        serializer.save(store=self.request.user.store)


class ActivityLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Per-store activity log. Supports ?user=&operation_type=&since= filters."""
    serializer_class = ActivityLogSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':     'MANAGER',
        'retrieve': 'MANAGER',
    }

    def get_queryset(self):
        qs = ActivityLog.objects.filter(store=self.request.user.store).select_related('user')
        params = self.request.query_params

        op = params.get('operation_type')
        if op:
            qs = qs.filter(operation_type=op)

        user_id = params.get('user')
        if user_id:
            qs = qs.filter(user_id=user_id)

        since = params.get('since')
        if since:
            qs = qs.filter(timestamp__gt=since)

        return qs


class ActivityLogMetaView(APIView):
    """Returns the per-store dropdown options for the log filters (users + operation types)."""
    permission_classes = [IsAuthenticated, IsManagerOrAbove]

    def get(self, request):
        store = request.user.store
        if not store:
            return Response({'users': [], 'operation_types': []})

        users = (
            User.objects
            .filter(store=store)
            .order_by('first_name', 'username')
            .values('id', 'username', 'first_name', 'last_name')
        )
        users_list = [
            {
                'id': u['id'],
                'username': u['username'],
                'name': (f"{u['first_name']} {u['last_name']}".strip()) or u['username'],
            }
            for u in users
        ]
        op_types = [{'value': v, 'label': l} for v, l in ActivityLog.OperationType.choices]
        return Response({'users': users_list, 'operation_types': op_types})


LOW_STOCK_THRESHOLD = 5


class DashboardView(APIView):
    permission_classes = [IsAuthenticated, IsCashierOrAbove]

    def get(self, request):
        store = request.user.store
        if not store:
            return Response({'detail': 'User has no store assigned.'}, status=status.HTTP_403_FORBIDDEN)

        from finance.models import SalesInvoice, WorkShift
        from inventory.models import StockLevel

        today = timezone.localdate()

        # Today's posted sales
        today_invoices = SalesInvoice.objects.filter(
            store=store,
            status=SalesInvoice.Status.POSTED,
            date__date=today,
            is_deleted=False,
        )
        today_agg = today_invoices.aggregate(
            total=Sum('grand_total'),
            count=Count('id'),
        )
        items_sold = today_invoices.aggregate(
            qty=Sum('items__quantity')
        )['qty'] or 0

        # Open shift
        open_shift_qs = WorkShift.objects.filter(store=store, status=WorkShift.Status.OPEN).first()
        open_shift = None
        if open_shift_qs:
            open_shift = {
                'id': str(open_shift_qs.id),
                'start_time': open_shift_qs.start_time,
                'starting_cash': str(open_shift_qs.starting_cash),
                'user': open_shift_qs.user.get_full_name() or open_shift_qs.user.username,
            }

        # Low stock
        low_stock = (
            StockLevel.objects
            .filter(
                branch__store=store,
                quantity__lte=LOW_STOCK_THRESHOLD,
                variant__is_deleted=False,
                variant__product__is_deleted=False,
            )
            .select_related('variant__product', 'branch')
            .order_by('quantity')[:10]
        )
        low_stock_data = [
            {
                'sku': sl.variant.sku,
                'product_name': sl.variant.product.name,
                'branch': sl.branch.name,
                'quantity': str(sl.quantity),
            }
            for sl in low_stock
        ]

        # Recent sales (last 8 posted)
        recent_sales_qs = (
            SalesInvoice.objects
            .filter(store=store, status=SalesInvoice.Status.POSTED, is_deleted=False)
            .select_related('customer')
            .order_by('-date')[:8]
        )
        recent_sales = [
            {
                'id': str(inv.id),
                'invoice_number': inv.invoice_number,
                'customer': inv.customer.name,
                'grand_total': str(inv.grand_total),
                'date': inv.date,
            }
            for inv in recent_sales_qs
        ]

        return Response({
            'today_sales_total': str(today_agg['total'] or 0),
            'today_invoices_count': today_agg['count'] or 0,
            'today_items_sold': float(items_sold),
            'open_shift': open_shift,
            'low_stock_count': len(low_stock_data),
            'low_stock_items': low_stock_data,
            'recent_sales': recent_sales,
        })


class LabelPresetViewSet(viewsets.ModelViewSet):
    serializer_class = LabelPresetSerializer
    permission_classes = [IsManagerOrAbove]

    role_map = {
        'list':    'CASHIER',
        'retrieve':'CASHIER',
        'create':  'MANAGER',
        'update':  'MANAGER',
        'partial_update': 'MANAGER',
        'destroy': 'MANAGER',
    }

    def get_queryset(self):
        return LabelPreset.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)
