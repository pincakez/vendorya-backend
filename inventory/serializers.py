from rest_framework import serializers
from .models import (
    Product, Category, Supplier, AttributeDefinition,
    ProductVariant, ProductAttribute, StockLevel, Tax, StockAdjustment
)

# --- BASIC SERIALIZERS ---
class TaxSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tax
        fields = ['id', 'name', 'rate']
        read_only_fields = ['id']

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'parent']

class SupplierSerializer(serializers.ModelSerializer):
    balance = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, default=0)

    class Meta:
        model = Supplier
        fields = ['id', 'name', 'contact_info', 'code_prefix', 'balance']

class AttributeDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeDefinition
        fields = ['id', 'name', 'key', 'input_type', 'options']

# --- VARIANT SERIALIZERS ---
class ProductAttributeSerializer(serializers.ModelSerializer):
    definition_name = serializers.CharField(source='definition.name', read_only=True)
    definition_key  = serializers.CharField(source='definition.key', read_only=True)

    class Meta:
        model = ProductAttribute
        fields = ['id', 'definition', 'definition_name', 'definition_key', 'value']

class StockLevelSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = StockLevel
        fields = ['id', 'branch', 'branch_name', 'quantity']

class ProductVariantSerializer(serializers.ModelSerializer):
    attributes   = ProductAttributeSerializer(many=True, read_only=True)
    stock_levels = StockLevelSerializer(many=True, read_only=True)
    total_stock  = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = ['id', 'product', 'sku', 'barcode',
                  'cost_price', 'sell_price',
                  'attributes', 'stock_levels', 'total_stock']

    def get_total_stock(self, obj):
        return sum(s.quantity for s in obj.stock_levels.all())

# --- PRODUCT LIST SERIALIZER (optimised for table) ---
class ProductListSerializer(serializers.ModelSerializer):
    category_name      = serializers.CharField(source='category.name', read_only=True)
    supplier_name      = serializers.CharField(source='supplier.name', read_only=True)
    total_stock        = serializers.SerializerMethodField()
    price_display      = serializers.SerializerMethodField()
    profit_display     = serializers.SerializerMethodField()
    attributes_summary = serializers.SerializerMethodField()
    default_variant_id = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'category_name', 'supplier_name',
            'total_stock', 'price_display', 'profit_display',
            'attributes_summary', 'default_variant_id',
        ]

    def get_default_variant_id(self, obj):
        v = obj.variants.first()
        return str(v.id) if v else None

    def get_total_stock(self, obj):
        return sum(
            s.quantity
            for v in obj.variants.all()
            for s in v.stock_levels.all()
        )

    def get_price_display(self, obj):
        prices = [v.sell_price for v in obj.variants.all()]
        if not prices:
            return str(obj.base_price)
        return str(min(prices)) if min(prices) == max(prices) else f"{min(prices)} – {max(prices)}"

    def get_profit_display(self, obj):
        profits = [(v.sell_price - v.cost_price) for v in obj.variants.all()]
        if not profits:
            return "0.00"
        return str(min(profits)) if min(profits) == max(profits) else f"{min(profits)} – {max(profits)}"

    def get_attributes_summary(self, obj):
        summary = {}
        for variant in obj.variants.all():
            for attr in variant.attributes.all():
                key = attr.definition.key
                summary.setdefault(key, set()).add(attr.value)
        return {k: sorted(v) for k, v in summary.items()}

# --- FULL PRODUCT SERIALIZER (for add/edit) ---
class ProductDetailSerializer(serializers.ModelSerializer):
    variants = ProductVariantSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = '__all__'


# --- STOCK ADJUSTMENT SERIALIZER ---
class StockAdjustmentSerializer(serializers.ModelSerializer):
    variant_sku      = serializers.CharField(source='variant.sku', read_only=True)
    product_name     = serializers.CharField(source='variant.product.name', read_only=True)
    branch_name      = serializers.CharField(source='branch.name', read_only=True)
    adjusted_by_name = serializers.SerializerMethodField()

    class Meta:
        model = StockAdjustment
        fields = [
            'id', 'variant', 'variant_sku', 'product_name',
            'branch', 'branch_name',
            'quantity_change', 'reason', 'notes',
            'adjusted_by', 'adjusted_by_name', 'created_at',
        ]
        read_only_fields = ['id', 'adjusted_by', 'created_at']

    def get_adjusted_by_name(self, obj):
        u = obj.adjusted_by
        return u.get_full_name() or u.username
