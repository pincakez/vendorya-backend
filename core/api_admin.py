import json
from datetime import timedelta
from django.db.models import Count, Q, Sum
from django.utils import timezone as tz
from django.http import HttpResponse
from rest_framework import viewsets, filters, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
from .models import Store, Branch, ActivityLog
from .serializers import AdminActivityLogSerializer
from users.models import User
from users.permissions import IsSuperAdmin
from .api_admin_serializers import (
    AdminStoreSerializer, AdminStoreCreateSerializer,
    AdminBranchSerializer, AdminBranchCreateSerializer, AdminUserSerializer,
)


class AdminStoreViewSet(viewsets.ModelViewSet):
    """Platform-wide store management. Super-admin only.

    POST uses the compound `AdminStoreCreateSerializer` (owner + store + main
    branch, atomically).  GET/PATCH use `AdminStoreSerializer`.
    """
    permission_classes = [IsSuperAdmin]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'owner__username']
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'create':
            return AdminStoreCreateSerializer
        return AdminStoreSerializer

    def get_queryset(self):
        return (
            Store.objects
            .filter(is_deleted=False)
            .select_related('owner')
            .annotate(
                branches_count=Count('branches', filter=Q(branches__is_deleted=False), distinct=True),
                staff_count=Count('staff', distinct=True),
            )
            .order_by('name')
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        store = serializer.save()
        # Re-serialize through the read serializer so the response matches list/retrieve.
        store = (
            self.get_queryset().filter(pk=store.pk).first()
        )
        return Response(AdminStoreSerializer(store).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        """Log suspend / reactivate to the activity log, capturing the reason.

        Suspending a store flips `is_active` off; the login gate then blocks all
        its users. The reason (sent as `suspend_reason`) is recorded for audit.
        """
        was_active = serializer.instance.is_active
        store = serializer.save()
        if was_active and not store.is_active:
            reason = (self.request.data.get('suspend_reason') or '').strip() or 'No reason provided'
            grace_days = self.request.data.get('suspend_grace_days', 0)
            try:
                grace_days = int(grace_days)
            except (TypeError, ValueError):
                grace_days = 0
            details = {'reason': reason, 'by': self.request.user.username}
            if grace_days > 0:
                details['grace_period_days'] = grace_days
                details['note'] = f'Grace period: {grace_days} days from suspension date. Manual follow-up required.'
            ActivityLog.objects.create(
                store=store, user=self.request.user,
                operation_type=ActivityLog.OperationType.OTHER,
                action="Store suspended by admin",
                details=details,
            )
        elif not was_active and store.is_active:
            ActivityLog.objects.create(
                store=store, user=self.request.user,
                operation_type=ActivityLog.OperationType.OTHER,
                action="Store reactivated by admin",
                details={'by': self.request.user.username},
            )


class AdminBranchViewSet(viewsets.ModelViewSet):
    """All branches across all stores. Super-admin only."""
    serializer_class = AdminBranchSerializer
    permission_classes = [IsSuperAdmin]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'store__name']
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'create':
            return AdminBranchCreateSerializer
        return AdminBranchSerializer

    def get_queryset(self):
        return (
            Branch.objects
            .filter(is_deleted=False)
            .select_related('store', 'address')
            .order_by('store__name', 'name')
        )


class AdminUserViewSet(viewsets.ModelViewSet):
    """Manage other super-admin accounts. Super-admin only."""
    serializer_class = AdminUserSerializer
    permission_classes = [IsSuperAdmin]
    filter_backends = [filters.SearchFilter]
    search_fields = ['username', 'first_name', 'last_name', 'email']
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        return User.objects.filter(is_superadmin=True).order_by('username')


class AdminActivityLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Cross-store activity log for super-admins. Filters: ?store=&user=&operation_type=&since=."""
    serializer_class = AdminActivityLogSerializer
    permission_classes = [IsSuperAdmin]

    def get_queryset(self):
        qs = ActivityLog.objects.all().select_related('user', 'store')
        params = self.request.query_params

        store_id = params.get('store')
        if store_id:
            qs = qs.filter(store_id=store_id)

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


class AdminStoreCodeCheckView(APIView):
    """GET /api/admin/stores/check-code/?code=120 — returns available true/false. Sudo only."""
    permission_classes = [IsSuperAdmin]

    def get(self, request):
        code = request.query_params.get('code', '').strip()
        if not code or not code.isdigit() or len(code) != 3:
            return Response({'detail': 'Provide a 3-digit code.'}, status=status.HTTP_400_BAD_REQUEST)
        taken = Store.objects.filter(store_code=code).exists()
        return Response({'code': code, 'available': not taken})


class AdminActivityLogMetaView(APIView):
    """Dropdown options for the global log filters (stores + operation types)."""
    permission_classes = [IsSuperAdmin]

    def get(self, request):
        stores = (
            Store.objects
            .filter(is_deleted=False)
            .order_by('name')
            .values('id', 'name')
        )
        op_types = [{'value': v, 'label': l} for v, l in ActivityLog.OperationType.choices]
        return Response({
            'stores':          [{'id': str(s['id']), 'name': s['name']} for s in stores],
            'operation_types': op_types,
        })


class AdminActivityLogPurgeView(APIView):
    """Audit-log retention purge (sudo only). Mirrors the
    `purge_old_activity_logs` management command, manual-trigger.

      GET  ?years=2  (or ?days=90) → preview: how many rows would be deleted.
      POST {years|days}            → delete them (batched), logs the purge.
    """
    permission_classes = [IsSuperAdmin]

    def _cutoff(self, request):
        days = request.query_params.get('days') or request.data.get('days')
        years = request.query_params.get('years') or request.data.get('years')
        if days not in (None, ''):
            d = int(days)
            if d < 1:
                raise ValueError("days must be >= 1")
            return tz.now() - timedelta(days=d), f"{d} day(s)"
        y = int(years) if years not in (None, '') else 2
        if y < 1:
            raise ValueError("years must be >= 1")
        return tz.now() - timedelta(days=y * 365), f"{y} year(s)"

    def get(self, request):
        try:
            cutoff, window = self._cutoff(request)
        except (ValueError, TypeError):
            return Response({'detail': 'Invalid retention window.'},
                            status=status.HTTP_400_BAD_REQUEST)
        count = ActivityLog.objects.filter(timestamp__lt=cutoff).count()
        return Response({'window': window, 'cutoff': cutoff.date().isoformat(),
                         'count': count})

    def post(self, request):
        try:
            cutoff, window = self._cutoff(request)
        except (ValueError, TypeError):
            return Response({'detail': 'Invalid retention window.'},
                            status=status.HTTP_400_BAD_REQUEST)
        deleted = 0
        while True:
            ids = list(ActivityLog.objects.filter(timestamp__lt=cutoff)
                       .values_list('pk', flat=True)[:5000])
            if not ids:
                break
            ActivityLog.objects.filter(pk__in=ids).delete()
            deleted += len(ids)
        # Note: not self-logged — ActivityLog requires a store FK, and a
        # cross-store retention purge has no single store to attribute it to.
        return Response({'deleted': deleted, 'window': window,
                         'cutoff': cutoff.date().isoformat()})


class AdminStoreForceLogoutView(APIView):
    """POST /api/admin/stores/{store_id}/force-logout/

    Kill switch for compromised stores: blacklists every active refresh token
    for all users in the store. Their next API call will be rejected and they
    will be forced to log in again from scratch.
    """
    permission_classes = [IsSuperAdmin]

    def post(self, request, store_id):
        try:
            store = Store.objects.get(pk=store_id, is_deleted=False)
        except Store.DoesNotExist:
            return Response({'detail': 'Store not found.'}, status=status.HTTP_404_NOT_FOUND)

        user_ids = list(User.objects.filter(store=store).values_list('id', flat=True))

        # All outstanding tokens for this store's users that are still valid
        # and not already blacklisted.
        tokens = list(
            OutstandingToken.objects
            .filter(user_id__in=user_ids, expires_at__gt=tz.now())
            .exclude(blacklistedtoken__isnull=False)
        )
        count = len(tokens)
        if count:
            BlacklistedToken.objects.bulk_create(
                [BlacklistedToken(token=t) for t in tokens],
                ignore_conflicts=True,
            )

        ActivityLog.objects.create(
            store=store,
            user=request.user,
            operation_type=ActivityLog.OperationType.OTHER,
            action="Force-logout: all store sessions terminated",
            details={
                'by': request.user.username,
                'sessions_killed': count,
                'users_affected': len(user_ids),
            },
        )

        return Response({
            'detail': f'Terminated {count} active session(s) across {len(user_ids)} user(s).',
            'sessions_killed': count,
            'users_affected': len(user_ids),
        })


class AdminStoreUsageView(APIView):
    """GET /api/admin/stores/{store_id}/usage/ — tenant usage snapshot."""
    permission_classes = [IsSuperAdmin]

    def get(self, request, store_id):
        try:
            store = Store.objects.get(pk=store_id, is_deleted=False)
        except Store.DoesNotExist:
            return Response({'detail': 'Store not found.'}, status=status.HTTP_404_NOT_FOUND)

        from inventory.models import Product, ProductVariant
        from finance.models import SalesInvoice, PurchaseInvoice
        from users.models import User as U

        now = tz.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        thirty_days_ago = now - timedelta(days=30)

        staff_count = U.objects.filter(store=store, is_active=True).count()

        # DAU = distinct users with activity log entry today
        dau = (ActivityLog.objects
               .filter(store=store, timestamp__gte=day_start)
               .values('user').distinct().count())

        # MAU = distinct users active in last 30 days
        mau = (ActivityLog.objects
               .filter(store=store, timestamp__gte=thirty_days_ago)
               .values('user').distinct().count())

        products_count = Product.all_objects.filter(store=store, is_deleted=False).count()
        variants_count = ProductVariant.all_objects.filter(product__store=store, is_deleted=False).count()

        invoices_total = SalesInvoice.all_objects.filter(store=store, is_deleted=False).count()
        invoices_month = SalesInvoice.all_objects.filter(
            store=store, is_deleted=False, created_at__gte=month_start
        ).count()
        revenue_month = (SalesInvoice.all_objects
                         .filter(store=store, is_deleted=False, created_at__gte=month_start)
                         .aggregate(t=Sum('total'))['t'] or 0)

        purchases_total = PurchaseInvoice.all_objects.filter(store=store, is_deleted=False).count()

        branches_count = Branch.objects.filter(store=store, is_deleted=False).count()

        return Response({
            'store_id':       str(store.pk),
            'store_name':     store.name,
            'staff_count':    staff_count,
            'branches_count': branches_count,
            'dau':            dau,
            'mau':            mau,
            'products_count': products_count,
            'variants_count': variants_count,
            'invoices_total': invoices_total,
            'invoices_month': invoices_month,
            'revenue_month':  str(revenue_month),
            'purchases_total': purchases_total,
            'as_of':          now.isoformat(),
        })


class AdminStoreExportView(APIView):
    """GET /api/admin/stores/{store_id}/export/ — full tenant data export (GDPR / offboarding)."""
    permission_classes = [IsSuperAdmin]

    def get(self, request, store_id):
        try:
            store = Store.objects.get(pk=store_id, is_deleted=False)
        except Store.DoesNotExist:
            return Response({'detail': 'Store not found.'}, status=status.HTTP_404_NOT_FOUND)

        from inventory.models import Product, ProductVariant, Supplier, Category
        from finance.models import SalesInvoice, PurchaseInvoice, Expense
        from users.models import User as U

        def _qs_to_list(qs, fields):
            return list(qs.values(*fields))

        payload = {
            'export_meta': {
                'store_id':   str(store.pk),
                'store_name': store.name,
                'store_code': store.store_code,
                'exported_at': tz.now().isoformat(),
                'exported_by': request.user.username,
            },
            'staff': _qs_to_list(
                U.objects.filter(store=store),
                ['id', 'username', 'email', 'first_name', 'last_name', 'role', 'is_active', 'date_joined']
            ),
            'suppliers': _qs_to_list(
                Supplier.all_objects.filter(store=store, is_deleted=False),
                ['id', 'name', 'code_prefix', 'phone_number', 'email', 'created_at']
            ),
            'categories': _qs_to_list(
                Category.all_objects.filter(store=store, is_deleted=False),
                ['id', 'name', 'parent_id', 'created_at']
            ),
            'products': _qs_to_list(
                Product.all_objects.filter(store=store, is_deleted=False),
                ['id', 'name', 'supplier_id', 'category_id', 'created_at']
            ),
            'variants': _qs_to_list(
                ProductVariant.all_objects.filter(product__store=store, is_deleted=False),
                ['id', 'sku', 'product_id', 'sell_price', 'cost_price', 'created_at']
            ),
            'sales_invoices': _qs_to_list(
                SalesInvoice.all_objects.filter(store=store, is_deleted=False),
                ['id', 'invoice_number', 'status', 'total', 'created_at']
            ),
            'purchase_invoices': _qs_to_list(
                PurchaseInvoice.all_objects.filter(store=store, is_deleted=False),
                ['id', 'invoice_number', 'total', 'created_at']
            ),
            'expenses': _qs_to_list(
                Expense.all_objects.filter(store=store, is_deleted=False),
                ['id', 'description', 'amount', 'created_at']
            ),
        }

        def default_serializer(obj):
            import uuid, decimal
            if isinstance(obj, (uuid.UUID,)):
                return str(obj)
            if isinstance(obj, decimal.Decimal):
                return float(obj)
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        filename = f"vendorya_export_{store.store_code}_{tz.now().strftime('%Y%m%d_%H%M')}.json"
        content = json.dumps(payload, default=default_serializer, indent=2, ensure_ascii=False)
        response = HttpResponse(content, content_type='application/json')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
