from rest_framework import viewsets, permissions, filters
from .models import Product, Category, Supplier, AttributeDefinition
from .serializers import (
    ProductSerializer, 
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
    serializer_class = ProductSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'product_code']
    ordering_fields = ['name', 'price', 'stock_quantity']

    def get_queryset(self):
        # Optimization: prefetch attributes to avoid N+1 queries
        # This loads all dynamic data in ONE database call instead of hundreds
        return Product.objects.filter(
            store=self.request.user.store
        ).prefetch_related('attributes__definition', 'category', 'supplier')

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