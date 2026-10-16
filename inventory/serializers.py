from decimal import Decimal
from rest_framework import serializers
from django.db.models import Sum
from core.field_visibility import FieldVisibilityMixin, hides_cost, hides_supplier, request_user
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from inventory.fields import ShopVariantField
from .models import (
    Product, Category, Supplier, AttributeDefinition,
    ProductVariant, ProductAttribute, ProductUnit, StockLevel, Tax, StockAdjustment,
    StockTransfer, StockTransferItem,
    StorageLocation, StorageStock, StorageMovement,
    ProductMedia,
    is_multi_unit_enabled, is_weight_selling_enabled,
    is_expiry_tracked, draw_from_batches, restock_to_batch,
)


def build_selling_units(variant, product):
    """Unified list of sellable units for a variant: the implicit base unit
    first (factor 1, price = variant.sell_price, name = product.unit), followed
    by any opt-in alternate units. A single-unit product returns a 1-element
    list, so the POS knows not to show a unit picker.

    Alternate units are only included when the store's multi-unit master switch
    (StoreSettings.multi_unit_enabled, default ON) is on — off hides them so the
    product sells as a single base unit, while the ProductUnit rows stay in the DB.

    Weight products (Phase C, selling_mode=WEIGHT, gated on weight_selling_enabled)
    sell only in kilograms: the single returned unit is the kg base itself (factor 1,
    price = sell_price per kg) flagged is_weight so the POS does decimal entry. Packs
    are suppressed — weight and packs are mutually exclusive."""
    if (getattr(product, 'selling_mode', 'UNIT') == 'WEIGHT'
            and is_weight_selling_enabled(product.store_id)):
        return [{
            'id': None,
            'name': 'kg',
            'factor': '1.000',
            'price': str(variant.sell_price) if variant else str(product.base_price),
            'barcode': variant.barcode if variant else None,
            'is_base': True,
            'is_weight': True,
        }]
    units = [{
        'id': None,
        'name': product.unit or 'pcs',
        'factor': '1.000',
        'price': str(variant.sell_price) if variant else str(product.base_price),
        'barcode': variant.barcode if variant else None,
        'is_base': True,
        'is_weight': False,
        # Whether the base unit may be SOLD (single pills). Stock is still counted in
        # it regardless — display/decomposition uses every unit; POS filters on this.
        'sellable': bool(getattr(product, 'sell_base_unit', True)),
    }]
    if variant and is_multi_unit_enabled(product.store_id):
        for u in variant.selling_units.filter(is_deleted=False).order_by('sort_order', 'name'):
            units.append({
                'id': str(u.id),
                'name': u.name,
                'factor': str(u.factor),
                'price': str(u.sell_price),
                'barcode': u.barcode,
                'is_base': False,
                'is_weight': False,
                'sellable': bool(getattr(u, 'sellable', True)),
            })
    return units

# --- BASIC SERIALIZERS ---
class TaxSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tax
        fields = ['id', 'name', 'rate']
        read_only_fields = ['id']

class CategorySerializer(serializers.ModelSerializer):
    # Own spec-set lines (Upgrade 9) — from CategoryViewSet.get_queryset's annotation; 0 on a fresh row.
    spec_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Category
        fields = ['id', 'name', 'parent', 'spec_count']

class SupplierSerializer(serializers.ModelSerializer):
    # All three come from SupplierViewSet.get_queryset annotations (running
    # account): balance = received_total − payments_total. default=0 covers
    # serializing a freshly-created instance that carries no annotation.
    balance = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, default=0)
    received_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, default=0)
    payments_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, default=0)

    class Meta:
        model = Supplier
        fields = ['id', 'name', 'company_name', 'contact_info', 'code_prefix', 'prefix_locked',
                  'balance', 'received_total', 'payments_total',
                  'phone_number', 'whatsapp_number', 'email', 'instagram', 'website', 'country', 'city', 'notes']
        read_only_fields = ['id', 'prefix_locked']

    def validate_code_prefix(self, value):
        # s159: a NEW supplier's code has the shop's current width (2 or 3, +1 once a width is used up —
        # `inventory/sku.py`). Locked codes never change, so an edit is checked by the view, not here.
        if self.instance is not None:
            return value
        from inventory.sku import supplier_code_width
        request = self.context.get('request')
        store = getattr(getattr(request, 'user', None), 'store', None)
        if store is None:
            return value
        width = supplier_code_width(store)
        if not value.isdigit() or len(value) != width or value[0] == '0':
            raise serializers.ValidationError(f'Supplier code must be {width} digits (not starting with 0).')
        return value

class AttributeDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeDefinition
        fields = ['id', 'name', 'key', 'input_type', 'options', 'unit']

# --- VARIANT SERIALIZERS ---
class ProductAttributeSerializer(serializers.ModelSerializer):
    definition_name = serializers.CharField(source='definition.name', read_only=True)
    definition_key  = serializers.CharField(source='definition.key', read_only=True)

    class Meta:
        model = ProductAttribute
        fields = ['id', 'definition', 'definition_name', 'definition_key', 'value']

def _barcode_taken(code, owner):
    return _('Barcode %(code)s is already used by "%(name)s" in this shop.') % {'code': code, 'name': owner}


class ProductUnitSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductUnit
        fields = ['id', 'name', 'factor', 'sell_price', 'barcode', 'sort_order']
        read_only_fields = ['id']

class StockLevelSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = StockLevel
        fields = ['id', 'branch', 'branch_name', 'quantity']

class ProductVariantSerializer(serializers.ModelSerializer):
    attributes   = ProductAttributeSerializer(many=True, read_only=True)
    stock_levels = StockLevelSerializer(many=True, read_only=True)
    total_stock  = serializers.SerializerMethodField()
    storage_qty  = serializers.SerializerMethodField()
    product_name = serializers.CharField(source='product.name', read_only=True)
    track_serials = serializers.BooleanField(source='product.track_serials', read_only=True)   # New 7

    class Meta:
        model = ProductVariant
        fields = ['id', 'product', 'product_name', 'sku', 'sku2', 'barcode',
                  'cost_price', 'sell_price', 'reorder_level',
                  'attributes', 'stock_levels', 'total_stock', 'storage_qty', 'track_serials']

    def validate(self, attrs):
        from inventory.code_search import barcode_owner
        request = self.context.get('request')
        store = getattr(getattr(request, 'user', None), 'store', None)
        product = attrs.get('product') or (self.instance.product if self.instance else None)
        # A variant may only hang off a product of the caller's own shop.
        if product is not None and store is not None and product.store_id != store.id:
            raise serializers.ValidationError({'product': _('Product not found.')})
        # Barcode unique per shop, across variants AND packs (live STORE products only).
        if attrs.get('barcode') and product is not None and product.source == Product.Source.STORE:
            from inventory.code_search import lock_shop_barcodes
            lock_shop_barcodes(product.store)   # s166: a same-instant save waits, then sees this one
            owner = barcode_owner(product.store, attrs['barcode'],
                                  skip_variant_ids=[self.instance.id] if self.instance else [])
            if owner is not None:
                raise serializers.ValidationError({'barcode': _barcode_taken(attrs['barcode'], owner),
                                                   'barcode_code': attrs['barcode'], 'barcode_owner': owner})
        return attrs

    def get_total_stock(self, obj):
        return sum(s.quantity for s in obj.stock_levels.all())

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if hides_cost(request_user(self)):
            data.pop('cost_price', None)   # s167: no cost to a role the shop hides it from (cashier by default)
        return data

    def get_storage_qty(self, obj):
        # Sum of active (non-deleted) storage layers for this variant — the
        # off-floor quantity surfaced in the valuation table (Phase 5).
        total = (
            StorageStock.objects
            .filter(variant=obj, is_deleted=False)
            .aggregate(t=Sum('quantity_remaining'))['t']
        )
        return total or 0

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
    selling_units         = serializers.SerializerMethodField()
    sku_display           = serializers.SerializerMethodField()
    sku2_display          = serializers.SerializerMethodField()
    cost_display          = serializers.SerializerMethodField()
    # Enriched packaging from the linked Memory Base drug (§MB-COLS columns + modal auto-fill)
    strips_per_pack       = serializers.IntegerField(source='drug_profile.strips_per_pack', read_only=True)
    tablets_per_strip     = serializers.IntegerField(source='drug_profile.tablets_per_strip', read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'source', 'category', 'category_name', 'supplier_name',
            'total_stock', 'price_display', 'cost_display', 'profit_display',
            'attributes_summary', 'default_variant_id', 'default_variant_price',
            'default_variant_stock', 'selling_units', 'sku_display', 'sku2_display', 'hide_from_pos',
            'track_expiry', 'selling_mode', 'sell_base_unit',
            'track_serials', 'condition', 'warranty_months',
            'category_l1', 'category_l2', 'category_l3', 'category_l4',
            'strips_per_pack', 'tablets_per_strip',
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

    def get_selling_units(self, obj):
        return build_selling_units(obj.variants.first(), obj)

    def get_sku2_display(self, obj):
        v = next(iter(obj.variants.all()), None)
        return v.sku2 if v else None

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
class ProductMediaSerializer(serializers.ModelSerializer):
    """One showcase asset. `aspect` (w/h) lets the UI size the player — a 16:9
    clip goes wider, a 9:16 clip goes taller."""
    file_url   = serializers.SerializerMethodField()
    poster_url = serializers.SerializerMethodField()
    aspect     = serializers.SerializerMethodField()

    class Meta:
        model = ProductMedia
        fields = ['id', 'kind', 'file_url', 'poster_url',
                  'width', 'height', 'aspect', 'duration', 'order']

    def _url(self, f):
        # Relative ("/media/...") so it resolves against whatever origin the page
        # is on — correct in dev (vdev) and prod alike, no host/scheme guessing.
        return f.url if f else None

    def get_file_url(self, obj):
        return self._url(obj.file)

    def get_poster_url(self, obj):
        return self._url(obj.poster)

    def get_aspect(self, obj):
        return round(obj.width / obj.height, 4) if obj.width and obj.height else None


class ProductDetailSerializer(serializers.ModelSerializer):
    variants       = ProductVariantSerializer(many=True, read_only=True)
    category_name  = serializers.CharField(source='category.name', read_only=True)
    supplier_name  = serializers.CharField(source='supplier.name', read_only=True)
    supplier_contact = serializers.CharField(source='supplier.contact_info', read_only=True)
    image_url      = serializers.SerializerMethodField()
    media          = ProductMediaSerializer(many=True, read_only=True)
    selling_units  = serializers.SerializerMethodField()
    batches        = serializers.SerializerMethodField()
    # Enriched packaging from the linked Memory Base drug — feeds the edit-modal auto-fill
    strips_per_pack   = serializers.IntegerField(source='drug_profile.strips_per_pack', read_only=True)
    tablets_per_strip = serializers.IntegerField(source='drug_profile.tablets_per_strip', read_only=True)

    def get_image_url(self, obj):
        # Cover thumbnail: legacy single image first, else the first gallery photo
        # (prefetched on the viewset, so no extra query per row). Relative URL so it
        # resolves against the page origin in dev + prod.
        if obj.image:
            return obj.image.url
        first = next((m for m in obj.media.all() if m.kind == 'image'), None)
        return first.file.url if (first and first.file) else None

    def get_selling_units(self, obj):
        return build_selling_units(obj.variants.first(), obj)

    def get_batches(self, obj):
        """Open expiry batches (qty > 0) for this product's variants, FEFO order.
        Empty for non-tracked products — the UI hides the section accordingly."""
        if not obj.track_expiry:
            return []
        from .models import StockBatch
        rows = (StockBatch.objects
                .filter(variant__product=obj, quantity_remaining__gt=0)
                .select_related('branch', 'variant')
                .order_by('expiry_date', 'received_date'))
        today = timezone.now().date()
        out = []
        for b in rows:
            out.append({
                'id': str(b.id),
                'variant': str(b.variant_id),
                'sku': b.variant.sku,
                'branch': b.branch.name,
                'branch_id': str(b.branch_id),
                'batch_number': b.batch_number,
                'expiry_date': b.expiry_date.isoformat() if b.expiry_date else None,
                'quantity_remaining': str(b.quantity_remaining),
                'cost_per_base': str(b.cost_per_base),
                'is_expired': bool(b.expiry_date and b.expiry_date < today),
                'days_left': (b.expiry_date - today).days if b.expiry_date else None,
            })
        return out

    def to_representation(self, instance):
        data = super().to_representation(instance)
        user = request_user(self)
        if hides_cost(user):
            # s167: base price is a buying price too; batches carry their own cost. Variants strip themselves.
            data.pop('base_price', None)
            for b in data.get('batches') or []:
                b.pop('cost_per_base', None)
        if hides_supplier(user):
            for f in ('supplier', 'supplier_name', 'supplier_contact'):
                data.pop(f, None)
        return data

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
    selling_units = serializers.ListField(
        child=serializers.DictField(), write_only=True, required=False
    )
    # SKU2 (s159): only while the shop's SKU2 is ON. Blank on create = the auto one (if the shop has
    # "for new products" on); blank on edit = clear it.
    sku2 = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=8)

    class Meta:
        model = Product
        fields = ['id', 'name', 'description', 'category', 'supplier',
                  'base_price', 'attributes', 'cost_price', 'sell_price',
                  'reorder_level', 'unit', 'selling_units', 'track_expiry',
                  'selling_mode', 'sell_base_unit', 'sku2',
                  'track_serials', 'condition', 'warranty_months']

    def validate_sku2(self, value):
        from inventory.sku import sku2_active
        value = (value or '').strip()
        store = self.context['request'].user.store
        if not sku2_active(store):
            raise serializers.ValidationError(_('SKU2 is not switched on for this shop.'))
        if value and (not value.isdigit() or len(value) > 8):
            raise serializers.ValidationError(_('SKU2 must be 1 to 8 digits.'))
        if value:
            taken = ProductVariant.all_objects.filter(store=store, sku2=value)
            if self.instance is not None:
                taken = taken.exclude(product=self.instance)
            if taken.exists():
                raise serializers.ValidationError(_('Another product already has this SKU2.'))
        return value
        read_only_fields = ['id']

    def validate(self, data):
        """Phase C guards. Weight mode requires the store master switch and is
        mutually exclusive with pack units; we also force unit='kg' so displays
        and the POS label stay correct. When the master switch is off we silently
        fall back to UNIT so a stale flag can never strand a product."""
        mode = data.get('selling_mode')
        if mode is None and self.instance is not None:
            mode = self.instance.selling_mode
        if mode == Product.SellingMode.WEIGHT:
            store = self.context['request'].user.store
            if not is_weight_selling_enabled(store.id):
                # Master switch off → ignore the weight request, keep it a unit product.
                data['selling_mode'] = Product.SellingMode.UNIT
            else:
                units = data.get('selling_units')
                if units:
                    raise serializers.ValidationError(
                        {'selling_units': _('A weight product is sold per kg only — '
                                            'it cannot also have pack/strip units.')})
                data['unit'] = 'kg'
        if data.get('selling_units'):
            self._check_unit_barcodes(data['selling_units'])
        if 'attributes' in (getattr(self, 'initial_data', None) or {}):
            self._check_spec_values(data)
        self._check_serial_tracking(data)
        return data

    def _check_spec_values(self, data):
        """Upgrade 9 (s168): a NUMBER attribute takes a number, and the category's REQUIRED specs
        (inherited ones included) must have a value — on create, or when an edit changes the category.
        On such an edit a spec not sent keeps its stored value; one sent blank is being cleared. Only
        runs when the request carries `attributes` (the product forms), so bulk edits / ghosting never
        trip over a product made before the spec set existed."""
        from .spec_sets import effective_spec_set, _parse_uuid
        store = self.context['request'].user.store
        sent = {}
        for a in data.get('attributes') or []:
            did = _parse_uuid(a.get('definition') or a.get('definition_id'))
            if did is not None:
                sent[str(did)] = str(a.get('value') if a.get('value') is not None else '').strip()
        if sent:
            for d in AttributeDefinition.objects.filter(store=store, id__in=list(sent),
                                                        input_type=AttributeDefinition.InputType.NUMBER):
                v = sent[str(d.id)]
                if not v:
                    continue
                try:
                    ok = Decimal(v).is_finite()
                except (ArithmeticError, ValueError):
                    ok = False
                if not ok:
                    raise serializers.ValidationError({
                        'attributes': _('"%(name)s" must be a number.') % {'name': d.name},
                        'not_a_number': d.name})
        # Required specs are enforced only on CREATE and on an edit that MOVES the product to another
        # category (Yakot's rule via Sam, s168): a plain edit (price, name…) of an older product that
        # predates the spec set must never be locked out. The form still shows the * as a hint.
        if self.instance is not None and (
                'category' not in data or data['category'] == self.instance.category):
            return
        category = data['category'] if 'category' in data else None
        required = [s for s in effective_spec_set(category) if s['required']] if category else []
        if not required:
            return
        stored = {}
        if self.instance is not None:
            variant = self.instance.variants.first()
            if variant is not None:
                stored = {str(a.definition_id): a.value for a in variant.attributes.all()}
        missing = [s['name'] for s in required
                   if not (sent[s['definition']] if s['definition'] in sent else stored.get(s['definition']))]
        if missing:
            raise serializers.ValidationError({
                'attributes': _('Please fill in the required specs: %(names)s') % {'names': ', '.join(missing)},
                'missing_specs': missing})

    def _check_serial_tracking(self, data):
        """New 7: a serial-tracked product is bought and sold one whole unit at a time — never by weight,
        and its single unit must be sellable. Switching tracking OFF is refused while units with a serial
        are still on the shelf (their serials would silently stop meaning anything)."""
        inst = self.instance
        track = data.get('track_serials', inst.track_serials if inst is not None else False)
        # s176: Serial numbers switch off → no NEW product starts tracking (products already tracked keep working).
        if track and not (inst is not None and inst.track_serials):
            from core.modules import is_on
            if not is_on(self.context['request'].user.store, 'serials'):
                raise serializers.ValidationError(
                    {'track_serials': _('Serial numbers are switched off in Settings → Modules.'),
                     'serial_code': 'serials_off'})
        if track:
            mode = data.get('selling_mode', inst.selling_mode if inst is not None else Product.SellingMode.UNIT)
            if mode == Product.SellingMode.WEIGHT:
                raise serializers.ValidationError(
                    {'track_serials': _('A product sold by weight cannot track serial numbers.'),
                     'serial_code': 'serial_weight'})
            if not data.get('sell_base_unit', inst.sell_base_unit if inst is not None else True):
                raise serializers.ValidationError(
                    {'track_serials': _('A product with serial numbers is sold one unit at a time — '
                                        'keep "sell by base unit" on.'),
                     'serial_code': 'serial_base_unit'})
        elif inst is not None and inst.track_serials:
            from inventory.serials import has_units_on_shelf
            if has_units_on_shelf(inst):
                raise serializers.ValidationError(
                    {'track_serials': _('Units with a serial number are still in stock — sell them, write them '
                                        'off or remove their serials before switching tracking off.'),
                     'serial_code': 'serial_still_in_stock'})

    def _check_unit_barcodes(self, units):
        """A pack barcode is unique per shop, across variants AND packs (live STORE products only).
        This product's own current packs don't count — the save rewrites or drops all of them —
        but its variant's own barcode does (a scan must mean one thing)."""
        from inventory.code_search import barcode_owner
        store = self.context['request'].user.store
        if self.instance is not None and self.instance.source != Product.Source.STORE:
            return
        variant = self.instance.variants.first() if self.instance is not None else None
        skip_units = list(variant.selling_units.values_list('id', flat=True)) if variant else []
        if any((u.get('barcode') or '').strip() for u in units):
            from inventory.code_search import lock_shop_barcodes
            lock_shop_barcodes(store)   # s166: a same-instant save waits, then sees this one
        seen = set()
        for u in units:
            if not (u.get('name') or '').strip() or u.get('factor') in (None, '', 0, '0'):
                continue   # _sync_selling_units skips these rows too
            code = (u.get('barcode') or '').strip()
            if not code:
                continue
            # barcode_code / barcode_owner let the screen word the error in the user's language.
            if code in seen:
                raise serializers.ValidationError({
                    'selling_units': _('Barcode %(code)s is used by two packs of this product.') % {'code': code},
                    'barcode_code': code})
            seen.add(code)
            owner = barcode_owner(store, code, skip_unit_ids=skip_units)
            if owner is not None:
                raise serializers.ValidationError({'selling_units': _barcode_taken(code, owner),
                                                   'barcode_code': code, 'barcode_owner': owner})

    def _sync_selling_units(self, variant, units_data):
        """Reconcile a variant's opt-in alternate units against the desired list.
        Existing rows are matched by id (update), missing ids are soft-deleted,
        id-less rows are created. The base unit is implicit and never appears here."""
        if variant is None or units_data is None:
            return
        keep_ids = set()
        for u in units_data:
            uid    = u.get('id')
            name   = (u.get('name') or '').strip()
            factor = u.get('factor')
            if not name or factor in (None, '', 0, '0'):
                continue   # skip blank/invalid rows
            defaults = {
                'name': name,
                'factor': factor,
                'sell_price': u.get('sell_price') or 0,
                'barcode': u.get('barcode') or None,
                'sort_order': u.get('sort_order') or 0,
                # Per-product "show this tier in the POS picker"; default True keeps
                # older clients that don't send the flag fully sellable.
                'sellable': bool(u.get('sellable', True)),
            }
            if uid:
                ProductUnit.objects.filter(id=uid, variant=variant).update(**defaults)
                keep_ids.add(str(uid))
            else:
                obj = ProductUnit.objects.create(variant=variant, **defaults)
                keep_ids.add(str(obj.id))
        # Soft-delete any unit the client dropped from the list.
        for existing in variant.selling_units.filter(is_deleted=False):
            if str(existing.id) not in keep_ids:
                existing.is_deleted = True
                existing.save(update_fields=['is_deleted'])

    def create(self, validated_data):
        from .product_service import create_product_with_variant
        attrs      = validated_data.pop('attributes', [])
        cost_price = validated_data.pop('cost_price', 0)
        sell_price = validated_data.pop('sell_price', 0)
        reorder    = validated_data.pop('reorder_level', None)
        units      = validated_data.pop('selling_units', None)
        sku2       = validated_data.pop('sku2', None)

        store    = validated_data.pop('store')
        name     = validated_data.pop('name')
        supplier = validated_data.pop('supplier', None)
        category = validated_data.pop('category', None)
        base     = validated_data.pop('base_price', 0)

        product = create_product_with_variant(
            store, name=name, supplier=supplier, category=category,
            base_price=base, cost_price=cost_price, sell_price=sell_price,
            attributes=attrs, reorder_level=reorder,
            # any remaining Product fields (e.g. description, tax, unit) pass through
            extra_product_fields=validated_data,
        )
        self._sync_selling_units(product.variants.first(), units)
        if sku2:
            variant = product.variants.first()
            variant.sku2 = sku2
            variant.save(update_fields=['sku2', 'updated_at'])
        return product

    def update(self, instance, validated_data):
        from django.db import transaction
        attrs      = validated_data.pop('attributes', None)
        cost_price = validated_data.pop('cost_price', None)
        sell_price = validated_data.pop('sell_price', None)
        reorder    = validated_data.pop('reorder_level', None)
        units      = validated_data.pop('selling_units', None)
        sku2       = validated_data.pop('sku2', None)
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
                if sku2 is not None:
                    variant.sku2 = sku2 or None
                variant.save()

                self._sync_selling_units(variant, units)

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
    variant = ShopVariantField()   # s175: this shop's variants only
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
            'batch_expiry_date', 'batch_number', 'serial_numbers',
            'adjusted_by', 'adjusted_by_name', 'created_at',
        ]
        read_only_fields = ['id', 'adjusted_by', 'created_at']

    def get_fields(self):
        fields = super().get_fields()
        # New 7: the units added / removed, one serial each (required for a serial-tracked product).
        from finance.serializers import SerialListField
        fields['serial_numbers'] = SerialListField(required=False)
        return fields

    def validate(self, attrs):
        """A serial-tracked product moves by serial: one serial per unit, whole units only (the
        units are applied in StockAdjustment.save, in the same locked transaction as the stock)."""
        from inventory import serials as serial_engine
        variant = attrs.get('variant')
        if variant is not None and serial_engine.is_tracked(variant):
            name = variant.product.name
            need = serial_engine.whole_units(name, abs(Decimal(str(attrs.get('quantity_change') or 0))))
            serial_engine._check_count(name, attrs.get('serial_numbers') or [], need)
        else:
            attrs['serial_numbers'] = []
        return attrs

    def get_adjusted_by_name(self, obj):
        u = obj.adjusted_by
        return u.get_full_name() or u.username


# --- STOCK TRANSFER SERIALIZERS ---
class StockTransferItemSerializer(serializers.ModelSerializer):
    variant = ShopVariantField()   # s175: this shop's variants only
    variant_sku  = serializers.CharField(source='variant.sku', read_only=True)
    product_name = serializers.CharField(source='variant.product.name', read_only=True)

    class Meta:
        model = StockTransferItem
        fields = ['id', 'variant', 'variant_sku', 'product_name', 'quantity', 'serial_numbers']
        read_only_fields = ['id']

    def get_fields(self):
        fields = super().get_fields()
        from finance.serializers import SerialListField
        fields['serial_numbers'] = SerialListField(required=False)   # New 7: the units that travel
        return fields


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
            from inventory import serials as serial_engine
            for item_data in items_data:
                if not serial_engine.is_tracked(item_data['variant']):
                    item_data['serial_numbers'] = []
                ti = StockTransferItem.objects.create(transfer=transfer, **item_data)
                # New 7: serial-tracked units travel by serial (one per unit, IN_STOCK at the source).
                serial_engine.transfer_item(ti, transfer)
                variant, qty = item_data['variant'], Decimal(str(item_data['quantity']))
                # Deduct from source
                src, _ = StockLevel.objects.select_for_update().get_or_create(
                    variant=variant, branch=transfer.from_branch
                )
                src.quantity = Decimal(str(src.quantity)) - qty
                src.save()
                # Add to destination (a brand-new row's default is a float → Decimal, s157: it crashed)
                dst, _ = StockLevel.objects.select_for_update().get_or_create(
                    variant=variant, branch=transfer.to_branch
                )
                dst.quantity = Decimal(str(dst.quantity)) + qty
                dst.save()
                if is_expiry_tracked(variant):
                    self._move_batches(variant, transfer, qty)
        return transfer

    @staticmethod
    def _move_batches(variant, transfer, qty):
        """s157 (§AUDIT B3): batches travel with the stock — FEFO out of the source, each part landing at the
        destination with the same expiry / lot / cost. Any part the source's batches can't cover (old
        un-batched stock) lands as unknown-expiry, so both branches' batches still add up to their totals."""
        moved = Decimal('0')
        for d in draw_from_batches(variant, transfer.from_branch, qty):
            b = d['batch']
            restock_to_batch(variant, transfer.to_branch, transfer.store, d['qty'],
                             expiry_date=b.expiry_date, batch_number=b.batch_number,
                             cost_per_base=d['cost_per_base'])
            moved += d['qty']
        if qty - moved > 0:
            restock_to_batch(variant, transfer.to_branch, transfer.store, qty - moved)


# --- STORAGE SERIALIZERS ---
class StorageLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = StorageLocation
        fields = ['id', 'name', 'description', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']


class StorageStockSerializer(serializers.ModelSerializer):
    variant_sku           = serializers.CharField(source='variant.sku', read_only=True)
    product_name          = serializers.CharField(source='variant.product.name', read_only=True)
    storage_location_name = serializers.CharField(source='storage_location.name', read_only=True)
    days_in_storage       = serializers.SerializerMethodField()

    class Meta:
        model = StorageStock
        fields = [
            'id', 'variant', 'variant_sku', 'product_name',
            'storage_location', 'storage_location_name',
            'quantity_remaining', 'cost_at_move', 'moved_in_at', 'days_in_storage',
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if hides_cost(request_user(self)):
            data.pop('cost_at_move', None)   # s167: storage contents is open to cashiers
        return data

    def get_days_in_storage(self, obj):
        return (timezone.now() - obj.moved_in_at).days


class StorageMovementSerializer(serializers.ModelSerializer):
    variant_sku           = serializers.CharField(source='variant.sku', read_only=True)
    product_name          = serializers.CharField(source='variant.product.name', read_only=True)
    storage_location_name = serializers.CharField(source='storage_location.name', read_only=True)
    direction_display     = serializers.CharField(source='get_direction_display', read_only=True)
    from_branch_name      = serializers.CharField(source='from_branch.name', read_only=True, default=None)
    to_branch_name        = serializers.CharField(source='to_branch.name', read_only=True, default=None)
    created_by_name       = serializers.SerializerMethodField()

    class Meta:
        model = StorageMovement
        fields = [
            'id', 'variant', 'variant_sku', 'product_name',
            'storage_location', 'storage_location_name',
            'direction', 'direction_display', 'quantity', 'cost_at_move',
            'from_branch', 'from_branch_name', 'to_branch', 'to_branch_name',
            'moved_at', 'created_by', 'created_by_name',
            'reason', 'note', 'related_adjustment',
        ]
        read_only_fields = fields

    def get_created_by_name(self, obj):
        u = obj.created_by
        return (u.get_full_name() or u.username) if u else None
