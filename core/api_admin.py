from django.db.models import Count, Q
from rest_framework import viewsets, filters, status
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import Store, Branch, ActivityLog
from .serializers import AdminActivityLogSerializer
from users.models import User
from users.permissions import IsSuperAdmin
from .api_admin_serializers import (
    AdminStoreSerializer, AdminStoreCreateSerializer,
    AdminBranchSerializer, AdminUserSerializer,
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


class AdminBranchViewSet(viewsets.ModelViewSet):
    """All branches across all stores. Super-admin only."""
    serializer_class = AdminBranchSerializer
    permission_classes = [IsSuperAdmin]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'store__name']
    http_method_names = ['get', 'patch', 'head', 'options']

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
