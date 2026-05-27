from django.db.models import Count, Q
from rest_framework import viewsets, filters
from .models import Store, Branch
from users.models import User
from users.permissions import IsSuperAdmin
from .api_admin_serializers import (
    AdminStoreSerializer, AdminBranchSerializer, AdminUserSerializer,
)


class AdminStoreViewSet(viewsets.ModelViewSet):
    """Platform-wide store management. Super-admin only."""
    serializer_class = AdminStoreSerializer
    permission_classes = [IsSuperAdmin]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'owner__username']
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

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
