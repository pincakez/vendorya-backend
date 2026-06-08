import uuid
import random as _random
from django.db import models, transaction
from django.core.validators import RegexValidator
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
    name = models.CharField(_("Supplier Name"), max_length=255)
    contact_info = models.TextField(_("Contact Info"), blank=True, null=True)
    
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