from rest_framework import serializers
from .models import Product, Category, Supplier, AttributeDefinition, ProductAttribute

class AttributeDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeDefinition
        fields = ['id', 'name', 'key', 'input_type', 'options']

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
    
    # This field will hold our dynamic columns data
    dynamic_attributes = serializers.SerializerMethodField()
    
    class Meta:
        model = Product
        fields = [
            'id', 'name', 'product_code', 'category', 'category_name',
            'supplier', 'supplier_name', 'status', 'description',
            'wholesale_price', 'price', 'stock_quantity', 'profit',
            'dynamic_attributes', 'created_at'
        ]
        read_only_fields = ['product_code', 'profit', 'created_at']

    def get_dynamic_attributes(self, obj):
        """
        Converts related ProductAttribute rows into a simple dictionary.
        Example output: { "season": "Spring", "gender": "Male" }
        """
        # We use the 'key' from the definition as the dictionary key
        return {attr.definition.key: attr.value for attr in obj.attributes.all()}

    def create(self, validated_data):
        # Extract dynamic attributes from the request if present (for saving)
        # Note: This requires the frontend to send 'dynamic_attributes' as a dict
        dynamic_data = self.context['request'].data.get('dynamic_attributes', {})
        product = super().create(validated_data)
        
        self._save_attributes(product, dynamic_data)
        return product

    def update(self, instance, validated_data):
        dynamic_data = self.context['request'].data.get('dynamic_attributes', {})
        product = super().update(instance, validated_data)
        
        self._save_attributes(product, dynamic_data)
        return product

    def _save_attributes(self, product, dynamic_data):
        """Helper to save dynamic attributes."""
        if not dynamic_data:
            return

        # Get all definitions for this store to validate keys
        definitions = {d.key: d for d in AttributeDefinition.objects.filter(store=product.store)}
        
        for key, value in dynamic_data.items():
            if key in definitions:
                ProductAttribute.objects.update_or_create(
                    product=product,
                    definition=definitions[key],
                    defaults={'value': str(value)}
                )