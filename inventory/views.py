from rest_framework import viewsets, permissions, filters
from .models import Product, Category, Supplier, AttributeDefinition, ProductVariant
from .serializers import (
    ProductListSerializer, ProductDetailSerializer,
    ProductVariantSerializer,
    CategorySerializer, SupplierSerializer, AttributeDefinitionSerializer,
)

class AttributeDefinitionViewSet(viewsets.ModelViewSet):
    serializer_class = AttributeDefinitionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return AttributeDefinition.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class ProductViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
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
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ProductVariant.objects.filter(
            product__store=self.request.user.store
        ).prefetch_related('attributes', 'attributes__definition', 'stock_levels')


class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Category.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class SupplierViewSet(viewsets.ModelViewSet):
    serializer_class = SupplierSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Supplier.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)
