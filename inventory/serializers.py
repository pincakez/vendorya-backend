from rest_framework import serializers
from .models import Product, Category, Supplier

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'parent']

class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ['id', 'name', 'contact_info']

class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    
    class Meta:
        model = Product
        fields = [
            'id', 'name', 'product_code', 'category', 'category_name',
            'supplier', 'supplier_name', 'status', 'description',
            'wholesale_price', 'price', 'stock_quantity', 'profit',
            'attributes', 'created_at'
        ]
        read_only_fields = ['product_code', 'profit', 'created_at']