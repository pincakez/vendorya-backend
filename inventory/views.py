from decimal import Decimal
from django.db.models import Q, Sum, F, Min, Value, OuterRef, Subquery, ExpressionWrapper, DecimalField
from django.db.models.functions import Coalesce
from rest_framework import viewsets, filters, status
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from users.permissions import RoleScopedPermission, IsManagerOrAbove
from .models import Product, Category, Supplier, AttributeDefinition, ProductVariant, Tax, StockAdjustment
from .serializers import (
    ProductListSerializer, ProductDetailSerializer, ProductWriteSerializer,
    ProductVariantSerializer,
    CategorySerializer, SupplierSerializer, AttributeDefinitionSerializer, TaxSerializer,
    StockAdjustmentSerializer,
)
from core.activity import log_activity
from core.models import ActivityLog
from core.field_visibility import hidden_fields_for

# Maps a sort key -> the output field it would reveal the order of.
_ORDER_TO_FIELD = {'o_wholesale': 'cost_display', 'o_retail': 'price_display', 'o_profit': 'profit_display'}


class VisibilityOrderingFilter(filters.OrderingFilter):
    """Drop ordering by fields the user isn't permitted to see (prevents
    inferring a hidden column's order). Pairs with FieldVisibilityMixin."""
    def remove_invalid_fields(self, queryset, fields, view, request):
        valid = super().remove_invalid_fields(queryset, fields, view, request)
        hidden = hidden_fields_for(request.user, getattr(view, 'fv_table_id', None))
        if not hidden:
            return valid
        blocked = {k for k, f in _ORDER_TO_FIELD.items() if f in hidden}
        return [t for t in valid if t.lstrip('-') not in blocked]


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
    filter_backends = [filters.SearchFilter, VisibilityOrderingFilter]
    fv_table_id = 'inventory_products'
    search_fields = ['name', 'variants__sku', 'variants__barcode']
    # Server-side sort. FE maps column keys -> these (see Products.vue ORDER_MAP).
    ordering_fields = ['name', 'supplier__name', 'created_at',
                       'o_sku', 'o_wholesale', 'o_retail', 'o_profit', 'o_stock']

    # Reserved params that are NOT dynamic attribute filters.
    _RESERVED_PARAMS = {'search', 'ordering', 'page', 'page_size', 'category'}

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return ProductWriteSerializer
        if self.action == 'retrieve':
            return ProductDetailSerializer
        return ProductListSerializer

    def get_queryset(self):
        qs = Product.objects.filter(store=self.request.user.store).prefetch_related(
            'category', 'supplier',
            'variants', 'variants__stock_levels',
            'variants__attributes', 'variants__attributes__definition',
        )

        # Sort annotations. Min over variants is fan-out-safe (idempotent); stock
        # uses a subquery so the Sum isn't multiplied by the variant join.
        stock_sq = Subquery(
            ProductVariant.objects.filter(product=OuterRef('pk'))
            .values('product')
            .annotate(t=Coalesce(Sum('stock_levels__quantity'), Value(Decimal('0'))))
            .values('t')[:1]
        )
        qs = qs.annotate(
            o_sku=Min('variants__sku'),
            o_wholesale=Min('variants__cost_price'),
            o_retail=Min('variants__sell_price'),
            o_stock=Coalesce(stock_sq, Value(Decimal('0')), output_field=DecimalField()),
        ).annotate(
            o_profit=ExpressionWrapper(F('o_retail') - F('o_wholesale'), output_field=DecimalField()),
        )

        params = self.request.query_params

        # Category quick-filter
        category = params.get('category')
        if category:
            qs = qs.filter(category_id=category)

        # Dynamic attribute filters: ?season=AW25&gender=Men
        for key, value in params.items():
            if key not in self._RESERVED_PARAMS:
                qs = qs.filter(variants__attributes__definition__key=key,
                               variants__attributes__value=value).distinct()
        return qs

    def perform_create(self, serializer):
        from billing.quota import enforce_quota
        enforce_quota(self.request.user.store, 'products')
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
        # Lock the prefix on creation — irreversible by design
        serializer.save(store=self.request.user.store, prefix_locked=True)

    def perform_update(self, serializer):
        instance = self.get_object()
        # Prevent changing the prefix once locked
        if instance.prefix_locked and 'code_prefix' in serializer.validated_data:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'code_prefix': 'Supplier prefix is locked and cannot be changed.'})
        serializer.save()


class SupplierPrefixCheckView(APIView):
    """GET /api/inventory/suppliers/check-prefix/?prefix=101 — store-scoped availability check."""
    permission_classes = [IsAuthenticated, IsManagerOrAbove]

    def get(self, request):
        prefix = request.query_params.get('prefix', '').strip()
        if not prefix or not prefix.isdigit() or len(prefix) != 3:
            return Response({'detail': 'Provide a 3-digit prefix.'}, status=status.HTTP_400_BAD_REQUEST)
        taken = Supplier.objects.filter(store=request.user.store, code_prefix=prefix).exists()
        return Response({'prefix': prefix, 'available': not taken})


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
