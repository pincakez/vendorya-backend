from rest_framework import serializers
from .models import Product, Category, Supplier, AttributeDefinition, ProductVariant, StockLevel

# --- BASIC SERIALIZERS ---
class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'parent']

class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ['id', 'name', 'contact_info', 'code_prefix']

class AttributeDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeDefinition
        fields = ['id', 'name', 'key', 'input_type', 'options']

# --- PRODUCT LIST SERIALIZER (Optimized for Table) ---
class ProductListSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    
    total_stock = serializers.SerializerMethodField()
    price_display = serializers.SerializerMethodField()
    profit_display = serializers.SerializerMethodField()
    attributes_summary = serializers.SerializerMethodField()
    default_variant_id = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'category_name', 'supplier_name',
            'total_stock', 'price_display', 'profit_display', 'attributes_summary',
            'default_variant_id',
            'status' # Assuming Product has status field (SoftDeleteModel has is_deleted, maybe add status later)
        ]

    def get_default_variant_id(self, obj):
        v = obj.variants.first()
        return v.id if v else None

    def get_total_stock(self, obj):
        total = 0
        for variant in obj.variants.all():
            for stock in variant.stock_levels.all():
                total += stock.quantity
        return total

    def get_price_display(self, obj):
        variants = obj.variants.all()
        if not variants:
            return str(obj.base_price)
        prices = [v.sell_price for v in variants]
        min_p, max_p = min(prices), max(prices)
        return str(min_p) if min_p == max_p else f"{min_p} - {max_p}"

    def get_profit_display(self, obj):
        variants = obj.variants.all()
        if not variants:
            return "0.00"
        profits = [(v.sell_price - v.cost_price) for v in variants]
        min_p, max_p = min(profits), max(profits)
        return str(min_p) if min_p == max_p else f"{min_p} - {max_p}"

    def get_attributes_summary(self, obj):
        summary = {}
        for variant in obj.variants.all():
            for attr in variant.attributes.all():
                key = attr.definition.name
                if key not in summary: summary[key] = set()
                summary[key].add(attr.value)
        return {k: ", ".join(sorted(v)) for k, v in summary.items()}

# --- FULL PRODUCT SERIALIZER (For Add/Edit Page) ---
# We will need this later for the "One-Page" form
class ProductDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = '__all__'