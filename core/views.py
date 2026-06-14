import os
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from django.utils import timezone
from django.db import connection
from django.db.models import Sum, Count, F, DecimalField
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
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

    @action(detail=True, methods=['get'], url_path='detail-data')
    def detail_data(self, request, pk=None):
        from finance.models import SalesInvoice
        from inventory.models import StockLevel

        branch = self.get_object()
        today = timezone.localdate()

        today_qs = SalesInvoice.objects.filter(
            store=request.user.store,
            branch=branch,
            status=SalesInvoice.Status.POSTED,
            date__date=today,
        )
        today_agg = today_qs.aggregate(total=Sum('grand_total'), count=Count('id'))

        staff = [
            {
                'id': str(u.id),
                'username': u.username,
                'full_name': f"{u.first_name} {u.last_name}".strip() or u.username,
                'role': u.role,
            }
            for u in User.objects.filter(store=request.user.store, default_branch=branch)
        ]

        levels = list(StockLevel.objects.filter(
            branch=branch,
            variant__product__store=request.user.store,
            variant__product__is_deleted=False,
        ).select_related('variant'))

        total_units = sum(sl.quantity for sl in levels)
        variants_in_stock = sum(1 for sl in levels if sl.quantity > 0)
        low_stock_count = sum(
            1 for sl in levels
            if sl.quantity <= (sl.variant.reorder_level or 5)
        )

        return Response({
            'branch': BranchSerializer(branch).data,
            'today_sales': {
                'total': str(today_agg['total'] or 0),
                'count': today_agg['count'] or 0,
            },
            'staff': staff,
            'stock_summary': {
                'variants_in_stock': int(variants_in_stock),
                'total_units': float(total_units),
                'low_stock_count': int(low_stock_count),
            },
        })


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


class DashboardView(APIView):
    permission_classes = [IsAuthenticated, IsCashierOrAbove]

    def get(self, request):
        store = request.user.store
        if not store:
            return Response({'detail': 'User has no store assigned.'}, status=status.HTTP_403_FORBIDDEN)

        from finance.models import SalesInvoice, WorkShift
        from inventory.models import StockLevel, StorageStock

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

        # Low stock — per-variant reorder_level (falls back to 5 by default)
        low_stock = (
            StockLevel.objects
            .filter(
                branch__store=store,
                quantity__lte=F('variant__reorder_level'),
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

        # Inventory value — active (on-floor) + storage (off-floor, still owned).
        # Storage is an operational visibility filter, not an accounting boundary:
        # the balance-sheet figure is active + storage.
        _dec = DecimalField(max_digits=18, decimal_places=2)
        active_value = (
            StockLevel.objects
            .filter(
                branch__store=store,
                variant__is_deleted=False,
                variant__product__is_deleted=False,
            )
            .aggregate(v=Sum(F('quantity') * F('variant__cost_price'), output_field=_dec))['v']
            or 0
        )
        storage_value = (
            StorageStock.objects
            .filter(store=store, is_deleted=False)
            .aggregate(v=Sum(F('quantity_remaining') * F('cost_at_move'), output_field=_dec))['v']
            or 0
        )

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

        # Upcoming services (next 5 with ETA, not Done/Archived)
        from services.models import Service
        upcoming_services_qs = (
            Service.objects
            .filter(
                store=store,
                no_eta=False,
                eta_datetime__isnull=False,
                status=Service.Status.OPEN,
                is_deleted=False,
            )
            .select_related('client')
            .order_by('eta_datetime')[:5]
        )
        upcoming_services = [
            {
                'id': str(svc.id),
                'serial_number': svc.serial_number,
                'client_name': svc.client.name if svc.client_id else svc.client_name or '—',
                'service_type': svc.service_type,
                'eta_datetime': svc.eta_datetime,
                'cost': str(svc.cost),
            }
            for svc in upcoming_services_qs
        ]

        return Response({
            'today_sales_total': str(today_agg['total'] or 0),
            'today_invoices_count': today_agg['count'] or 0,
            'today_items_sold': float(items_sold),
            'open_shift': open_shift,
            'low_stock_count': len(low_stock_data),
            'low_stock_items': low_stock_data,
            'inventory_value_active': str(active_value),
            'inventory_value_storage': str(storage_value),
            'inventory_value_total': str(active_value + storage_value),
            'recent_sales': recent_sales,
            'upcoming_services': upcoming_services,
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


class HealthView(APIView):
    """GET /api/health/ — public endpoint for uptime checks and the UI status dot."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        db_ok = True
        try:
            connection.ensure_connection()
        except Exception:
            db_ok = False
        s = 'ok' if db_ok else 'degraded'
        code = status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response({'status': s, 'db': db_ok, 'ts': timezone.now().isoformat()}, status=code)


# ── QZ Tray certificate signing ──────────────────────────────────────────────

def _load_private_key():
    raw = os.environ.get('QZTRAY_PRIVATE_KEY', '')
    if not raw:
        return None
    pem = raw.replace('\\n', '\n').encode()
    try:
        return serialization.load_pem_private_key(pem, password=None)
    except Exception:
        return None


class QZTrayCertView(APIView):
    """Return the public certificate so the frontend can pass it to QZ Tray."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # The cert lives under the frontend's downloads folder. On prod ONLY the
        # built `dist/` is deployed (no `public/`); on WSL dev Vite serves `public/`.
        # Try both so the endpoint returns the cert in either environment — if it
        # returns empty, QZ Tray sees an "anonymous request" and can't be trusted.
        base = os.path.join(os.path.dirname(__file__), '..', '..', 'vendorya-frontend')
        for sub in ('dist', 'public'):
            cert_path = os.path.normpath(os.path.join(base, sub, 'downloads', 'vendorya-qztray-cert.pem'))
            try:
                with open(cert_path) as f:
                    return Response({'certificate': f.read()})
            except FileNotFoundError:
                continue
        return Response({'certificate': ''})


class QZTraySignView(APIView):
    """Sign a QZ Tray challenge string with the RSA private key."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        to_sign = request.data.get('toSign', '')
        if not to_sign:
            return Response({'detail': 'toSign is required.'}, status=status.HTTP_400_BAD_REQUEST)
        key = _load_private_key()
        if not key:
            return Response({'detail': 'Signing key not configured.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        sig = key.sign(to_sign.encode(), padding.PKCS1v15(), hashes.SHA512())
        return Response({'signature': base64.b64encode(sig).decode()})
