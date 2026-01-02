from rest_framework import viewsets, permissions, filters
from .models import Product, Category, Supplier, AttributeDefinition
from .serializers import (
    ProductListSerializer, # We will use this for the list
    CategorySerializer, 
    SupplierSerializer, 
    AttributeDefinitionSerializer
)

class AttributeDefinitionViewSet(viewsets.ModelViewSet):
    """API to manage the dynamic columns (definitions)."""
    serializer_class = AttributeDefinitionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return AttributeDefinition.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)

class ProductViewSet(viewsets.ModelViewSet):
    serializer_class = ProductListSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'variants__sku', 'variants__barcode']
    ordering_fields = ['name', 'created_at']

    def get_queryset(self):
        # CRITICAL OPTIMIZATION: Prefetch everything to avoid 1000 DB calls
        return Product.objects.filter(store=self.request.user.store).prefetch_related(
            'category', 
            'supplier', 
            'variants', 
            'variants__stock_levels',
            'variants__attributes',
            'variants__attributes__definition'
        )

    def perform_create(self, serializer):
        # Automatically assign the store when creating a product
        serializer.save(store=self.request.user.store)

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