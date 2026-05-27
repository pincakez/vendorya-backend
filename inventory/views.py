from decimal import Decimal
from django.db.models import Q, Sum, F, ExpressionWrapper, DecimalField
from django.db.models.functions import Coalesce
from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated
from users.permissions import RoleScopedPermission
from .models import Product, Category, Supplier, AttributeDefinition, ProductVariant, Tax, StockAdjustment
from .serializers import (
    ProductListSerializer, ProductDetailSerializer,
    ProductVariantSerializer,
    CategorySerializer, SupplierSerializer, AttributeDefinitionSerializer, TaxSerializer,
    StockAdjustmentSerializer,
)
from core.activity import log_activity
from core.models import ActivityLog


class AttributeDefinitionViewSet(viewsets.ModelViewSet):
    serializer_class = AttributeDefinitionSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'ADMIN',
        'update':         'ADMIN',
        'partial_update': 'ADMIN',
        'destroy':        'ADMIN',
    }

    def get_queryset(self):
        return AttributeDefinition.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class ProductViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'variants__sku', 'variants__barcode']
    ordering_fields = ['name', 'created_at']

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProductDetailSerializer
        return ProductListSerializer

    def get_queryset(self):
        qs = Product.objects.filter(store=self.request.user.store).prefetch_related(
            'category', 'supplier',
            'variants', 'variants__stock_levels',
            'variants__attributes', 'variants__attributes__definition',
        )
        # Dynamic attribute filters: ?season=AW25&gender=Men
        params = self.request.query_params
        for key, value in params.items():
            if key not in ('search', 'ordering', 'page', 'page_size'):
                qs = qs.filter(variants__attributes__definition__key=key,
                               variants__attributes__value=value).distinct()
        return qs

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class ProductVariantViewSet(viewsets.ModelViewSet):
    serializer_class = ProductVariantSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
    }

    def get_queryset(self):
        return ProductVariant.objects.filter(
            product__store=self.request.user.store
        ).prefetch_related('attributes', 'attributes__definition', 'stock_levels')


class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
    }

    def get_queryset(self):
        return Category.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class SupplierViewSet(viewsets.ModelViewSet):
    serializer_class = SupplierSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'MANAGER',
        'retrieve':       'MANAGER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
    }
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'contact_info']

    def get_queryset(self):
        outstanding = ExpressionWrapper(
            F('purchases__total_amount') - F('purchases__paid_amount'),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
        return (
            Supplier.objects
            .filter(store=self.request.user.store)
            .annotate(balance=Coalesce(
                Sum(outstanding, filter=Q(purchases__is_deleted=False)),
                Decimal('0'),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ))
        )

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class TaxViewSet(viewsets.ModelViewSet):
    serializer_class = TaxSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'ADMIN',
        'update':         'ADMIN',
        'partial_update': 'ADMIN',
        'destroy':        'ADMIN',
    }

    def get_queryset(self):
        return Tax.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class StockAdjustmentViewSet(viewsets.ModelViewSet):
    serializer_class = StockAdjustmentSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':     'MANAGER',
        'retrieve': 'MANAGER',
        'create':   'MANAGER',
    }
    http_method_names = ['get', 'post', 'head', 'options']  # immutable ledger — no edit/delete

    def get_queryset(self):
        return (
            StockAdjustment.objects
            .filter(store=self.request.user.store)
            .select_related('variant__product', 'branch', 'adjusted_by')
            .order_by('-created_at')
        )

    def perform_create(self, serializer):
        adjustment = serializer.save(store=self.request.user.store, adjusted_by=self.request.user)
        log_activity(
            request=self.request,
            action=f"Stock adjustment: {adjustment.variant.sku} ({adjustment.quantity_change:+})",
            op_type=ActivityLog.OperationType.ADJUSTMENT,
            details={
                'adjustment_id': str(adjustment.id),
                'sku': adjustment.variant.sku,
                'product': adjustment.variant.product.name,
                'change': str(adjustment.quantity_change),
                'reason': adjustment.reason,
            },
        )
