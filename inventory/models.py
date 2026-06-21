import uuid
import random as _random
from django.db import models, transaction
from django.core.validators import RegexValidator
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from core.models import TimestampedModel, SoftDeleteModel, Store, Branch
from core.tenancy import TenantScopedManager, TenantSoftDeleteManager

# --- 1. TAXATION ---
class Tax(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='taxes')
    name = models.CharField(_("Tax Name"), max_length=50)
    rate = models.DecimalField(_("Rate %"), max_digits=5, decimal_places=2)

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    def __str__(self):
        return f"{self.name} ({self.rate}%)"

# --- 2. CORE INVENTORY ---
class Supplier(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='suppliers')
    name            = models.CharField(_("Supplier Name"), max_length=255)
    company_name    = models.CharField(max_length=255, blank=True, default='')
    contact_info    = models.TextField(_("Contact Info"), blank=True, null=True)
    phone_number    = models.CharField(max_length=20, blank=True, default='')
    whatsapp_number = models.CharField(max_length=20, blank=True, default='')
    email           = models.EmailField(blank=True, default='')
    instagram       = models.CharField(max_length=100, blank=True, default='')
    website         = models.URLField(blank=True, default='')
    country         = models.CharField(max_length=100, blank=True, default='Egypt')
    city            = models.CharField(max_length=100, blank=True, default='')
    notes           = models.TextField(blank=True, default='')
    
    code_prefix = models.CharField(
        _("Supplier Code Prefix"),
        max_length=3,
        validators=[RegexValidator(r'^\d{3}$', _('Prefix must be exactly 3 digits (100–999).'))],
        help_text=_("Unique 3-digit code for this supplier within this store (100–999). Part of every SKU.")
    )
    prefix_locked = models.BooleanField(
        _("Prefix Locked"), default=False,
        help_text=_("Once locked the prefix can never be changed — it is embedded in all SKUs.")
    )

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    class Meta:
        unique_together = [('store', 'code_prefix')]

    def __str__(self):
        return f"{self.name} ({self.code_prefix})"

# Max category tree depth (tiers). e.g. Electronics > Computers > Laptops > Gaming
# = 4 tiers, then you stop and use product attributes for finer distinction.
MAX_CATEGORY_DEPTH = 4


class Category(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(_("Category Name"), max_length=150)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='subcategories')

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    def __str__(self):
        return f"{self.parent.name} > {self.name}" if self.parent else self.name

    # --- hierarchy guards: keep the tree sane (max depth + no cycles) -------
    @staticmethod
    def _ancestor_ids(start_parent):
        """Ids from `start_parent` up to the root (start_parent included)."""
        ids, node, guard = [], start_parent, 0
        while node is not None and guard < 100:
            ids.append(node.pk)
            node = node.parent
            guard += 1
        return ids

    def _subtree_height(self):
        """Edges down to the deepest active descendant (0 = leaf, no children)."""
        height = 0
        for child in Category.all_objects.filter(parent_id=self.pk, is_deleted=False):
            height = max(height, 1 + child._subtree_height())
        return height

    def clean(self):
        from django.core.exceptions import ValidationError
        if not self.parent_id:
            return
        parent = Category.all_objects.filter(pk=self.parent_id).first()
        if parent is None:
            raise ValidationError({'parent': 'Parent category does not exist.'})
        if parent.store_id != self.store_id:
            raise ValidationError({'parent': 'Parent category belongs to another store.'})

        chain = self._ancestor_ids(parent)
        # Cycle: a category cannot sit under itself or one of its own descendants.
        if self.pk in chain:
            raise ValidationError(
                {'parent': 'A category cannot sit under itself or its own sub-category.'})
        # Depth: parent's tier + this node + this node's existing subtree.
        deepest_tier = len(chain) + 1 + self._subtree_height()
        if deepest_tier > MAX_CATEGORY_DEPTH:
            raise ValidationError(
                {'parent': f'Categories can be at most {MAX_CATEGORY_DEPTH} levels deep.'})

    def save(self, *args, **kwargs):
        self.clean()   # single source of truth: API + AI tools + admin all pass here
        super().save(*args, **kwargs)

class AttributeDefinition(TimestampedModel, SoftDeleteModel):
    class InputType(models.TextChoices):
        TEXT = 'TEXT', _('Free Text')
        SELECT = 'SELECT', _('Dropdown Menu')
        NUMBER = 'NUMBER', _('Number')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='attribute_definitions')
    name = models.CharField(_("Display Name"), max_length=50)
    key = models.SlugField(_("Code Name"), max_length=50)
    input_type = models.CharField(max_length=20, choices=InputType.choices, default=InputType.TEXT)
    options = models.JSONField(default=list, blank=True)

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    def __str__(self):
        return self.name

# --- 3. PRODUCT & VARIANTS ---
class Product(TimestampedModel, SoftDeleteModel):
    class ProductType(models.TextChoices):
        STANDARD = 'STANDARD', _('Standard Product')
        SERVICE = 'SERVICE', _('Service (No Stock)')
        BUNDLE = 'BUNDLE', _('Bundle/Kit')

    class DeleteReason(models.TextChoices):
        DISCONTINUED = 'DISCONTINUED', _('Discontinued')
        DUPLICATE    = 'DUPLICATE',    _('Duplicate')
        MISTAKE      = 'MISTAKE',      _('Created by mistake')
        OTHER        = 'OTHER',        _('Other')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='products')
    name = models.CharField(_("Product Name"), max_length=255)
    product_type = models.CharField(max_length=20, choices=ProductType.choices, default=ProductType.STANDARD)

    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True)
    tax = models.ForeignKey(Tax, on_delete=models.SET_NULL, null=True, blank=True)

    description = models.TextField(blank=True, null=True)
    unit = models.CharField(_("Unit"), max_length=20, default="pcs")
    image = models.ImageField(upload_to='products/', null=True, blank=True)

    base_price = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # Ghost = hidden from POS only. Still fully searchable/editable everywhere else.
    hide_from_pos = models.BooleanField(
        _("Hide from POS"), default=False,
        help_text=_("Ghosted: stays in inventory and reports but won't appear in the POS catalog."),
    )

    # Opt-in expiry/batch (FEFO) tracking for THIS product. Only meaningful when the
    # store's StoreSettings.expiry_tracking_enabled master switch is ON (see
    # is_expiry_tracked()). A pharmacy turns this on for medicines but leaves it off
    # for chargers/tissues. Off = the product uses the simple single-number stock path
    # exactly as before. See StockBatch + the finance FEFO engine.
    track_expiry = models.BooleanField(
        _("Track Expiry / Batches"), default=False,
        help_text=_("Stock this product as dated batches and sell earliest-expiry-first (FEFO)."),
    )

    # Opt-in weight selling (Phase C). UNIT = the classic path (whole-piece qty,
    # +1 stepper, optional packs). WEIGHT = sold by weight: the base unit IS the
    # kilogram, quantities are decimal (Decimal(12,3) → exact gram precision,
    # 0.001 kg = 1 g), and the variant's sell_price is the price *per kg*. No ×1000
    # conversion anywhere — a weight line is just a normal base-unit line with a
    # decimal qty, so the whole stock/COGS/void engine is untouched. Only meaningful
    # when the store's StoreSettings.weight_selling_enabled master switch is ON (see
    # is_weight_selling_enabled). Mutually exclusive with ProductUnit packs.
    class SellingMode(models.TextChoices):
        UNIT   = 'UNIT',   _('By Unit (each)')
        WEIGHT = 'WEIGHT', _('By Weight (per kg)')

    selling_mode = models.CharField(
        _("Selling Mode"), max_length=10,
        choices=SellingMode.choices, default=SellingMode.UNIT,
    )

    # Whether single base units (e.g. one loose pill) may be SOLD. Stock is always
    # COUNTED in the base unit regardless — this only controls whether POS offers the
    # base unit as a sellable option. OFF ⇒ the smallest sellable unit is the first
    # tier (e.g. a strip); the base still appears in stock/breakdown displays. Only
    # meaningful when the store's multi_unit_enabled switch is ON.
    sell_base_unit = models.BooleanField(
        _("Sell by base unit"), default=True,
        help_text=_("Allow selling single base units (e.g. one pill) in POS. Stock is still counted in base units either way."),
    )

    # Soft-delete audit (captured on delete; free-text note for OTHER).
    delete_reason = models.CharField(max_length=20, choices=DeleteReason.choices, blank=True, default='')
    delete_note   = models.CharField(max_length=255, blank=True, default='')
    deleted_by    = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='deleted_products',
    )

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    def __str__(self):
        return self.name

class ProductVariant(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    sku = models.CharField(
        _("SKU"),
        max_length=10,
        unique=True,
        blank=True,
        validators=[RegexValidator(r'^\d{10}$', _('SKU must be exactly 10 digits.'))]
    )
    barcode = models.CharField(_("Barcode"), max_length=100, blank=True, null=True)

    cost_price = models.DecimalField(_("Cost (Avg)"), max_digits=12, decimal_places=2, default=0.00)
    sell_price = models.DecimalField(_("Sell Price"), max_digits=12, decimal_places=2, default=0.00)

    # Per-variant low-stock threshold. When on-hand qty (per branch) drops to/below
    # this, the variant shows in the dashboard "low stock" list and fires a
    # low-stock notification on sale. Replaces the old hardcoded store-wide 5.
    reorder_level = models.DecimalField(
        _("Reorder Level"), max_digits=12, decimal_places=3, default=5,
        help_text=_("Alert when on-hand stock falls to or below this. Default 5."))

    def __str__(self):
        return f"{self.product.name} ({self.sku})"

    def save(self, *args, **kwargs):
        if not self.sku:
            with transaction.atomic():
                self.sku = self._generate_sku()
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def _generate_sku(self):
        store    = self.product.store
        supplier = self.product.supplier

        if not store.store_code:
            raise ValueError("Store must have a store_code set before products can be created.")
        if not supplier:
            raise ValueError("Product must be assigned a supplier before a variant can be saved.")
        if not supplier.prefix_locked:
            raise ValueError("Supplier prefix must be confirmed (locked) before products can be created.")

        store_part    = store.store_code        # 3 digits
        supplier_part = supplier.code_prefix    # 3 digits
        prefix        = f"{store_part}{supplier_part}"

        with transaction.atomic():
            # Lock the supplier row — prevents concurrent SKU generation for same supplier
            Supplier.objects.select_for_update().get(pk=supplier.pk)

            existing_nums = set()
            for sku in ProductVariant.all_objects.filter(
                sku__startswith=prefix
            ).values_list('sku', flat=True):
                suffix = sku[6:]
                if suffix.isdigit():
                    existing_nums.add(int(suffix))

            try:
                mode = store.settings.product_numbering_mode
            except Exception:
                mode = 'PROGRESSIVE'

            if mode == 'RANDOM':
                available = set(range(1, 10000)) - existing_nums
                if not available:
                    raise ValueError("All 9999 product slots for this supplier are used.")
                counter = _random.choice(list(available))
            else:
                counter = max(existing_nums, default=0) + 1
                if counter > 9999:
                    raise ValueError("Maximum product count (9999) reached for this supplier.")

            return f"{prefix}{counter:04d}"

class ProductAttribute(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='attributes')
    definition = models.ForeignKey(AttributeDefinition, on_delete=models.PROTECT)
    value = models.CharField(max_length=255)

    class Meta:
        unique_together = ('variant', 'definition')

class ProductUnit(TimestampedModel, SoftDeleteModel):
    """Alternate selling unit for a variant (opt-in multi-UoM).

    Stock is ALWAYS stored in the variant's base unit (StockLevel.quantity).
    The base unit itself is implicit (the variant, factor 1, price = sell_price)
    and is NOT stored here — these rows are only the *extra* units a product
    chooses to sell in. e.g. a pharmacy: base = Tablet, then a Strip (factor 10)
    and a Pack (factor 30), each with its own sell price. A variant with zero
    ProductUnit rows behaves exactly as before (single base unit).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='selling_units')
    name = models.CharField(_("Unit Name"), max_length=30)   # "Strip", "Pack", "Case"
    factor = models.DecimalField(
        _("Conversion Factor"), max_digits=12, decimal_places=3,
        help_text=_("How many base units this equals (Strip=10, Pack=30)."))
    sell_price = models.DecimalField(_("Sell Price"), max_digits=12, decimal_places=2, default=0.00)
    barcode = models.CharField(_("Barcode"), max_length=100, blank=True, null=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f"{self.name} (×{self.factor})"

# --- 4. MULTI-WAREHOUSE STOCK ---
class StockLevel(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='stock_levels')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='stock_levels')
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0.000)
    
    class Meta:
        unique_together = ('variant', 'branch')

    def __str__(self):
        return f"{self.branch.name}: {self.quantity}"

# --- 4b. EXPIRY / BATCH TRACKING (FEFO) ---
def is_multi_unit_enabled(store_id):
    """True when this store offers multi-unit (pack/strip) selling on top of the
    base unit. Single master switch (StoreSettings.multi_unit_enabled), default ON
    so existing s97 multi-unit products keep working. Off → only the base unit is
    offered; ProductUnit rows stay in the DB but are not surfaced. Mirrors the
    per-call query style of is_expiry_tracked for consistency."""
    from core.models import StoreSettings
    return bool(
        StoreSettings.objects.filter(
            store_id=store_id, multi_unit_enabled=True
        ).values_list('id', flat=True).first()
    )


def is_weight_selling_enabled(store_id):
    """True when this store has the weight-selling master switch on
    (StoreSettings.weight_selling_enabled, default OFF — Phase C). Off → a
    product's selling_mode=WEIGHT is dormant and it behaves as a classic unit
    product everywhere. Mirrors is_multi_unit_enabled's per-call query style."""
    from core.models import StoreSettings
    return bool(
        StoreSettings.objects.filter(
            store_id=store_id, weight_selling_enabled=True
        ).values_list('id', flat=True).first()
    )


def is_expiry_tracked(variant):
    """True when this variant's stock should be managed as dated batches (FEFO).

    Two gates, both required (see CLAUDE design): the store's master switch
    (StoreSettings.expiry_tracking_enabled) AND the product's own track_expiry flag.
    Either off → the variant uses the classic single-number StockLevel path, so
    non-pharmacy stores are wholly unaffected.
    """
    product = variant.product
    if not getattr(product, 'track_expiry', False):
        return False
    from core.models import StoreSettings
    return bool(
        StoreSettings.objects.filter(
            store_id=product.store_id, expiry_tracking_enabled=True
        ).values_list('id', flat=True).first()
    )


class StockBatch(TimestampedModel):
    """A received lot of a variant at a branch, with its own expiry + remaining qty.

    Created only for expiry-tracked variants (opt-in). Quantities are in BASE units —
    consistent with StockLevel and the s97 UoM engine. StockLevel.quantity stays the
    authoritative cached on-hand total that the whole app reads; the sum of a
    variant+branch's open batches (quantity_remaining > 0) must equal it. Both move
    together inside the same locked transaction on every stock-touch point.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='stock_batches')
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='batches')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='stock_batches')

    batch_number = models.CharField(_("Batch / Lot No."), max_length=60, blank=True, default='')
    # Null only for "unknown expiry" stock (e.g. a positive count-correction or a
    # return whose origin batch is unknown). FEFO orders these last.
    expiry_date = models.DateField(_("Expiry Date"), null=True, blank=True)
    quantity_remaining = models.DecimalField(max_digits=12, decimal_places=3, default=0.000)
    cost_per_base = models.DecimalField(
        _("Cost / base unit"), max_digits=12, decimal_places=2, default=0.00,
        help_text=_("True-FEFO COGS source: the cost of one base unit in this batch."))
    received_date = models.DateField(default=timezone.now)
    # Provenance — which purchase line created this batch (null for adjustments/returns).
    source_purchase_item = models.ForeignKey(
        'finance.PurchaseItem', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_batches')

    class Meta:
        ordering = ['expiry_date', 'received_date']
        indexes = [
            models.Index(fields=['variant', 'branch', 'quantity_remaining']),
            models.Index(fields=['store', 'expiry_date']),
        ]

    def __str__(self):
        return f"{self.variant.sku} · exp {self.expiry_date or '—'} · {self.quantity_remaining} left"

    @property
    def is_expired(self):
        return bool(self.expiry_date and self.expiry_date < timezone.now().date())


def draw_from_batches(variant, branch, base_qty):
    """FEFO draw-down: remove `base_qty` BASE units from a variant's open batches at
    `branch`, earliest-expiry first (NULL expiry last). Returns the list of draws as
    [{batch, qty, cost_per_base, expired}], in draw order. Caller MUST be inside a
    transaction.atomic() — rows are locked with select_for_update.

    Does NOT touch StockLevel (the caller owns the cached total) and does NOT enforce
    the expired-sale policy (the checkout guard does that up front). If batches can't
    cover the full qty (only possible when negative stock is allowed), it draws what's
    there and the shortfall is simply uncosted — StockLevel still goes negative.
    """
    from decimal import Decimal
    from django.db.models import F
    remaining = Decimal(str(base_qty))
    draws = []
    batches = (StockBatch.objects.select_for_update()
               .filter(variant=variant, branch=branch, quantity_remaining__gt=0)
               .order_by(F('expiry_date').asc(nulls_last=True), 'received_date'))
    today = timezone.now().date()
    for b in batches:
        if remaining <= 0:
            break
        take = min(Decimal(str(b.quantity_remaining)), remaining)
        b.quantity_remaining = Decimal(str(b.quantity_remaining)) - take
        b.save(update_fields=['quantity_remaining', 'updated_at'])
        draws.append({
            'batch': b, 'qty': take, 'cost_per_base': Decimal(str(b.cost_per_base)),
            'expired': bool(b.expiry_date and b.expiry_date < today),
        })
        remaining -= take
    return draws


def restock_to_batch(variant, branch, store, base_qty, *, expiry_date=None,
                     batch_number='', cost_per_base=None, source_purchase_item=None,
                     prefer_batch=None):
    """Add `base_qty` BASE units back/into batch stock. Used by purchase receive (new
    batch), refund restock, void replay, and positive stock adjustments. Caller owns
    StockLevel. Must run inside transaction.atomic().

    Resolution order: an explicit `prefer_batch` (void replay → exact origin batch) →
    an existing open batch matching (expiry_date, batch_number) → a brand-new batch.
    """
    from decimal import Decimal
    base_qty = Decimal(str(base_qty))
    if prefer_batch is not None:
        b = StockBatch.objects.select_for_update().get(pk=prefer_batch.pk)
        b.quantity_remaining = Decimal(str(b.quantity_remaining)) + base_qty
        b.save(update_fields=['quantity_remaining', 'updated_at'])
        return b
    if cost_per_base is None:
        cost_per_base = Decimal(str(variant.cost_price or '0'))
    # Coalesce into an identical open batch (same expiry + lot) to avoid fragmentation;
    # a return with unknown expiry folds into the earliest existing open batch.
    match = (StockBatch.objects.select_for_update()
             .filter(variant=variant, branch=branch, expiry_date=expiry_date,
                     batch_number=batch_number or '')
             .order_by('received_date').first())
    if match:
        match.quantity_remaining = Decimal(str(match.quantity_remaining)) + base_qty
        match.save(update_fields=['quantity_remaining', 'updated_at'])
        return match
    return StockBatch.objects.create(
        store=store, variant=variant, branch=branch, batch_number=batch_number or '',
        expiry_date=expiry_date, quantity_remaining=base_qty, cost_per_base=cost_per_base,
        source_purchase_item=source_purchase_item,
    )


# --- 5. BUNDLES ---
class BundleItem(models.Model):
    bundle = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='bundle_contents')
    component = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=1)

# --- 6. STOCK ADJUSTMENTS ---
class StockAdjustment(TimestampedModel):
    """Manual correction of stock (Theft, Damage, Gift)."""
    class Reason(models.TextChoices):
        THEFT = 'THEFT', _('Theft / Loss')
        DAMAGE = 'DAMAGE', _('Damage')
        COUNT_CORRECTION = 'CORRECTION', _('Count Correction')
        GIFT = 'GIFT', _('Gift / Sample')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    
    quantity_change = models.DecimalField(max_digits=10, decimal_places=3, help_text="Use negative for loss (e.g., -1), positive for gain.")
    reason = models.CharField(max_length=20, choices=Reason.choices)
    notes = models.TextField(blank=True)

    # Expiry-tracked products only: a positive gain needs a batch to live in. Captured
    # from the UI when the variant is expiry-tracked; ignored otherwise. A negative
    # change draws FEFO from existing batches (no expiry needed).
    batch_expiry_date = models.DateField(null=True, blank=True)
    batch_number = models.CharField(max_length=60, blank=True, default='')
    
    # Who did it?
    adjusted_by = models.ForeignKey('users.User', on_delete=models.PROTECT)

    objects     = TenantScopedManager()   # secure-by-default
    all_objects = models.Manager()        # unscoped escape hatch (sudo/audit/commands)

    def save(self, *args, **kwargs):
        # One source of truth for stock safety: a manual adjustment must obey the
        # store's allow_negative_stock policy exactly like a POS sale does. The
        # whole thing is atomic + row-locked, so if the policy blocks it nothing
        # is persisted (neither this ledger row nor the stock move).
        from decimal import Decimal
        from django.core.exceptions import ValidationError
        with transaction.atomic():
            super().save(*args, **kwargs)
            stock, created = (StockLevel.objects.select_for_update()
                              .get_or_create(variant=self.variant, branch=self.branch))
            # A freshly defaulted StockLevel.quantity can come back as a float;
            # coerce so float + Decimal never blows up.
            new_quantity = Decimal(str(stock.quantity)) + self.quantity_change
            if new_quantity < 0:
                # Read the policy fresh (avoid a stale cached reverse relation).
                from core.models import StoreSettings
                allow_negative = (
                    StoreSettings.objects.filter(store_id=self.store_id)
                    .values_list('allow_negative_stock', flat=True).first()
                ) or False
                if not allow_negative:
                    raise ValidationError(
                        f"Adjustment would drop stock to {new_quantity} at "
                        f"{self.branch.name}, but this store does not allow "
                        f"negative stock. Available: {stock.quantity}."
                    )
            stock.quantity = new_quantity
            stock.save()

            # Keep batch stock in lock-step with the cached total for tracked variants.
            if is_expiry_tracked(self.variant):
                change = Decimal(str(self.quantity_change))
                if change < 0:
                    draw_from_batches(self.variant, self.branch, -change)
                elif change > 0:
                    restock_to_batch(
                        self.variant, self.branch, self.store, change,
                        expiry_date=self.batch_expiry_date,
                        batch_number=self.batch_number,
                    )


# --- 7. STOCK TRANSFERS ---
class StockTransfer(TimestampedModel):
    """Move stock from one branch to another — instant, no pending state."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='stock_transfers')
    from_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='transfers_out')
    to_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='transfers_in')
    transferred_by = models.ForeignKey('users.User', on_delete=models.PROTECT)
    notes = models.TextField(blank=True)

    objects     = TenantScopedManager()   # secure-by-default
    all_objects = models.Manager()        # unscoped escape hatch (sudo/audit/commands)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Transfer {self.from_branch} → {self.to_branch} ({self.created_at:%Y-%m-%d})"


class StockTransferItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transfer = models.ForeignKey(StockTransfer, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)


# --- 8. STORAGE (operational visibility filter) ---------------------------
# Storage parks inactive stock off the floor: out of POS, low-stock alerts and
# live "active" views, but still owned and valued on the balance sheet. Moving
# to/from storage NEVER touches P&L or cost basis — only a write-off does (via
# a StockAdjustment(DAMAGE)). See Storageplan.md for the accounting audit.

class StorageLocation(TimestampedModel, SoftDeleteModel):
    """A named place to park stock. Branch-agnostic — one storage can serve
    multiple branches. Each store auto-gets a default 'Storage' on migration."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='storage_locations')
    name = models.CharField(_("Storage Name"), max_length=100)
    description = models.TextField(_("Description"), blank=True, null=True)
    is_active = models.BooleanField(_("Active"), default=True)

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    def __str__(self):
        return self.name


class StorageStock(TimestampedModel, SoftDeleteModel):
    """One row per move-in event (a *cost layer*), NOT a rolling quantity.

    Layers are required for accurate aging (days per batch), correct AVCO cost
    snapshots (frozen at the moment of move) and FIFO retrieval/write-off.
    `quantity_remaining` shrinks as a layer is consumed; an emptied layer is
    soft-deleted. `cost_at_move` never changes after creation."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='storage_stock')
    storage_location = models.ForeignKey(StorageLocation, on_delete=models.PROTECT, related_name='layers')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name='storage_layers')

    quantity_remaining = models.DecimalField(_("Qty Remaining"), max_digits=12, decimal_places=3)
    cost_at_move = models.DecimalField(_("Cost Snapshot"), max_digits=12, decimal_places=2)
    moved_in_at = models.DateTimeField(_("Moved In At"), default=timezone.now)

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    class Meta:
        ordering = ['moved_in_at']        # FIFO by default

    def __str__(self):
        return f"{self.variant.sku} @ {self.storage_location.name}: {self.quantity_remaining}"


class StorageMovement(TimestampedModel):
    """Immutable audit log of every storage move. Insert-only — never mutated.
    Drives the storage movement-history report and reconciliation."""
    class Direction(models.TextChoices):
        TO_STORAGE   = 'TO_STORAGE',   _('To Storage')
        FROM_STORAGE = 'FROM_STORAGE', _('From Storage')
        WRITE_OFF    = 'WRITE_OFF',    _('Write-Off')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='storage_movements')
    storage_location = models.ForeignKey(StorageLocation, on_delete=models.PROTECT, related_name='movements')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name='storage_movements')

    direction = models.CharField(max_length=20, choices=Direction.choices)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    cost_at_move = models.DecimalField(max_digits=12, decimal_places=2)

    from_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name='storage_moves_out')  # set on TO_STORAGE
    to_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True,
                                  related_name='storage_moves_in')     # set on FROM_STORAGE

    moved_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey('users.User', on_delete=models.PROTECT, related_name='storage_movements')
    reason = models.TextField(blank=True, null=True)
    note = models.TextField(blank=True, null=True)
    # Links a WRITE_OFF to its P&L document (the StockAdjustment).
    related_adjustment = models.ForeignKey(StockAdjustment, on_delete=models.SET_NULL,
                                           null=True, blank=True, related_name='storage_movements')

    objects     = TenantScopedManager()   # secure-by-default
    all_objects = models.Manager()        # unscoped escape hatch (sudo/audit/commands)

    class Meta:
        ordering = ['-moved_at']

    def __str__(self):
        return f"{self.get_direction_display()} {self.variant.sku} ×{self.quantity}"