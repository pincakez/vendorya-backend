from rest_framework import serializers
from core.field_visibility import FieldVisibilityMixin
from .models import (
    Product, Category, Supplier, AttributeDefinition,
    ProductVariant, ProductAttribute, StockLevel, Tax, StockAdjustment,
    StockTransfer, StockTransferItem,
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
        fields = ['id', 'name', 'contact_info', 'code_prefix', 'prefix_locked', 'balance']
        read_only_fields = ['id', 'prefix_locked']

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
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = ProductVariant
        fields = ['id', 'product', 'product_name', 'sku', 'barcode',
                  'cost_price', 'sell_price', 'reorder_level',
                  'attributes', 'stock_levels', 'total_stock']

    def get_total_stock(self, obj):
        return sum(s.quantity for s in obj.stock_levels.all())

# --- PRODUCT LIST SERIALIZER (optimised for table) ---
class ProductListSerializer(FieldVisibilityMixin, serializers.ModelSerializer):
    table_id = 'inventory_products'

    category_name      = serializers.CharField(source='category.name', read_only=True)
    # Per-tier category columns, derived from the product's single (deepest)
    # category by walking its ancestors. Always present (empty when shallower)
    # so the table's customize-columns can permit/toggle each level.
    category_l1        = serializers.SerializerMethodField()
    category_l2        = serializers.SerializerMethodField()
    category_l3        = serializers.SerializerMethodField()
    category_l4        = serializers.SerializerMethodField()
    supplier_name      = serializers.CharField(source='supplier.name', read_only=True)
    total_stock        = serializers.SerializerMethodField()
    price_display      = serializers.SerializerMethodField()
    profit_display     = serializers.SerializerMethodField()
    attributes_summary = serializers.SerializerMethodField()
    default_variant_id    = serializers.SerializerMethodField()
    default_variant_price = serializers.SerializerMethodField()
    default_variant_stock = serializers.SerializerMethodField()
    sku_display           = serializers.SerializerMethodField()
    cost_display          = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'category_name', 'supplier_name',
            'total_stock', 'price_display', 'cost_display', 'profit_display',
            'attributes_summary', 'default_variant_id', 'default_variant_price',
            'default_variant_stock', 'sku_display', 'hide_from_pos',
            'category_l1', 'category_l2', 'category_l3', 'category_l4',
        ]

    def _category_path(self, obj):
        """Names from root -> the product's category. Cached per instance.
        Relies on the viewset select_related'ing the parent chain (max 4 deep)."""
        cached = getattr(obj, '_cat_path_cache', None)
        if cached is not None:
            return cached
        names, node, guard = [], obj.category, 0
        while node is not None and guard < 10:
            names.append(node.name)
            node = node.parent
            guard += 1
        names.reverse()
        obj._cat_path_cache = names
        return names

    def get_category_l1(self, obj):
        p = self._category_path(obj); return p[0] if len(p) > 0 else ''
    def get_category_l2(self, obj):
        p = self._category_path(obj); return p[1] if len(p) > 1 else ''
    def get_category_l3(self, obj):
        p = self._category_path(obj); return p[2] if len(p) > 2 else ''
    def get_category_l4(self, obj):
        p = self._category_path(obj); return p[3] if len(p) > 3 else ''

    def get_default_variant_id(self, obj):
        v = obj.variants.first()
        return str(v.id) if v else None

    def get_default_variant_price(self, obj):
        v = obj.variants.first()
        return str(v.sell_price) if v else None

    def get_default_variant_stock(self, obj):
        v = obj.variants.first()
        if not v:
            return 0
        return sum(s.quantity for s in v.stock_levels.all())

    def get_sku_display(self, obj):
        variants = list(obj.variants.all())
        if not variants:
            return None
        if len(variants) == 1:
            return variants[0].sku
        return f"{variants[0].sku} +{len(variants) - 1}"

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

    def get_cost_display(self, obj):
        costs = [v.cost_price for v in obj.variants.all()]
        if not costs:
            return "0.00"
        return str(min(costs)) if min(costs) == max(costs) else f"{min(costs)} – {max(costs)}"

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
    variants       = ProductVariantSerializer(many=True, read_only=True)
    category_name  = serializers.CharField(source='category.name', read_only=True)
    supplier_name  = serializers.CharField(source='supplier.name', read_only=True)

    class Meta:
        model = Product
        fields = '__all__'


class ProductWriteSerializer(serializers.ModelSerializer):
    """Used for create / update. Accepts category/supplier FKs and creates the
    default variant inline when attributes or pricing are provided."""

    attributes = serializers.ListField(
        child=serializers.DictField(), write_only=True, required=False, default=list
    )
    cost_price = serializers.DecimalField(max_digits=12, decimal_places=2,
                                          write_only=True, required=False, default=0)
    sell_price = serializers.DecimalField(max_digits=12, decimal_places=2,
                                          write_only=True, required=False, default=0)
    reorder_level = serializers.DecimalField(max_digits=12, decimal_places=3,
                                             write_only=True, required=False)

    class Meta:
        model = Product
        fields = ['id', 'name', 'description', 'category', 'supplier',
                  'base_price', 'attributes', 'cost_price', 'sell_price',
                  'reorder_level']
        read_only_fields = ['id']

    def create(self, validated_data):
        from django.db import transaction
        attrs      = validated_data.pop('attributes', [])
        cost_price = validated_data.pop('cost_price', 0)
        sell_price = validated_data.pop('sell_price', 0)
        reorder    = validated_data.pop('reorder_level', None)

        with transaction.atomic():
            product = Product.objects.create(**validated_data)
            variant = ProductVariant.objects.create(
                product=product,
                cost_price=cost_price,
                sell_price=sell_price or validated_data.get('base_price', 0),
                **({'reorder_level': reorder} if reorder is not None else {}),
            )
            for attr in attrs:
                defn_id = attr.get('definition') or attr.get('definition_id')
                value   = attr.get('value', '')
                if defn_id and value:
                    defn = AttributeDefinition.objects.filter(
                        id=defn_id, store=product.store
                    ).first()
                    if defn:
                        ProductAttribute.objects.create(
                            variant=variant, definition=defn, value=value
                        )
        return product

    def update(self, instance, validated_data):
        from django.db import transaction
        attrs      = validated_data.pop('attributes', None)
        cost_price = validated_data.pop('cost_price', None)
        sell_price = validated_data.pop('sell_price', None)
        reorder    = validated_data.pop('reorder_level', None)
        # Supplier is locked after creation — the SKU embeds the supplier prefix,
        # so changing it would invalidate every SKU on this product.
        validated_data.pop('supplier', None)

        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()

            # Prices + attributes live on the default (first) variant. Stock
            # quantity is intentionally NOT touched here — it only moves via
            # purchases / sales / stock adjustments (each with a reason).
            variant = instance.variants.first()
            if variant:
                if cost_price is not None:
                    variant.cost_price = cost_price
                if sell_price is not None:
                    variant.sell_price = sell_price
                if reorder is not None:
                    variant.reorder_level = reorder
                variant.save()

                if attrs is not None:
                    for attr in attrs:
                        defn_id = attr.get('definition') or attr.get('definition_id')
                        value   = attr.get('value', '')
                        if not defn_id:
                            continue
                        defn = AttributeDefinition.objects.filter(
                            id=defn_id, store=instance.store
                        ).first()
                        if not defn:
                            continue
                        if value:
                            ProductAttribute.objects.update_or_create(
                                variant=variant, definition=defn,
                                defaults={'value': value},
                            )
                        else:
                            ProductAttribute.objects.filter(
                                variant=variant, definition=defn
                            ).delete()
        return instance


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


# --- STOCK TRANSFER SERIALIZERS ---
class StockTransferItemSerializer(serializers.ModelSerializer):
    variant_sku  = serializers.CharField(source='variant.sku', read_only=True)
    product_name = serializers.CharField(source='variant.product.name', read_only=True)

    class Meta:
        model = StockTransferItem
        fields = ['id', 'variant', 'variant_sku', 'product_name', 'quantity']
        read_only_fields = ['id']


class StockTransferSerializer(serializers.ModelSerializer):
    items               = StockTransferItemSerializer(many=True)
    from_branch_name    = serializers.CharField(source='from_branch.name', read_only=True)
    to_branch_name      = serializers.CharField(source='to_branch.name', read_only=True)
    transferred_by_name = serializers.SerializerMethodField()

    class Meta:
        model = StockTransfer
        fields = [
            'id', 'from_branch', 'from_branch_name',
            'to_branch', 'to_branch_name',
            'transferred_by', 'transferred_by_name',
            'notes', 'items', 'created_at',
        ]
        read_only_fields = ['id', 'transferred_by', 'created_at']

    def get_transferred_by_name(self, obj):
        u = obj.transferred_by
        return u.get_full_name() or u.username

    def validate(self, data):
        if data['from_branch'] == data['to_branch']:
            raise serializers.ValidationError("Source and destination branch must be different.")
        items = data.get('items', [])
        if not items:
            raise serializers.ValidationError("At least one item is required.")
        for item in items:
            if item['quantity'] <= 0:
                raise serializers.ValidationError("All quantities must be greater than zero.")
        return data

    def create(self, validated_data):
        from django.db import transaction
        from inventory.models import StockLevel
        items_data = validated_data.pop('items')
        with transaction.atomic():
            # Validate sufficient stock before touching anything
            for item_data in items_data:
                sl = StockLevel.objects.select_for_update().filter(
                    variant=item_data['variant'],
                    branch=validated_data['from_branch'],
                ).first()
                available = sl.quantity if sl else 0
                if available < item_data['quantity']:
                    raise serializers.ValidationError(
                        f"Insufficient stock for {item_data['variant'].sku}: "
                        f"available {available}, requested {item_data['quantity']}."
                    )

            transfer = StockTransfer.objects.create(**validated_data)
            for item_data in items_data:
                StockTransferItem.objects.create(transfer=transfer, **item_data)
                # Deduct from source
                src, _ = StockLevel.objects.get_or_create(
                    variant=item_data['variant'], branch=transfer.from_branch
                )
                src.quantity -= item_data['quantity']
                src.save()
                # Add to destination
                dst, _ = StockLevel.objects.get_or_create(
                    variant=item_data['variant'], branch=transfer.to_branch
                )
                dst.quantity += item_data['quantity']
                dst.save()
        return transfer
