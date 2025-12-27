import uuid
from django.db import models
from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _
from core.models import TimestampedModel, SoftDeleteModel, Store, Branch

# --- 1. TAXATION ---
class Tax(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='taxes')
    name = models.CharField(_("Tax Name"), max_length=50)
    rate = models.DecimalField(_("Rate %"), max_digits=5, decimal_places=2)
    
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
        max_length=2, 
        validators=[RegexValidator(r'^\d{2}$', 'Prefix must be exactly 2 digits (00-99).')],
        help_text=_("Unique 2-digit ID for generating product codes.")
    )
    
    class Meta:
        unique_together = [('store', 'code_prefix')]

    def __str__(self):
        return f"{self.name} ({self.code_prefix})"

class Category(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(_("Category Name"), max_length=150)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='subcategories')
    
    def __str__(self):
        return f"{self.parent.name} > {self.name}" if self.parent else self.name

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

    def __str__(self):
        return self.name

# --- 3. PRODUCT & VARIANTS ---
class Product(TimestampedModel, SoftDeleteModel):
    class ProductType(models.TextChoices):
        STANDARD = 'STANDARD', _('Standard Product')
        SERVICE = 'SERVICE', _('Service (No Stock)')
        BUNDLE = 'BUNDLE', _('Bundle/Kit')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='products')
    name = models.CharField(_("Product Name"), max_length=255)
    product_type = models.CharField(max_length=20, choices=ProductType.choices, default=ProductType.STANDARD)
    
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True)
    tax = models.ForeignKey(Tax, on_delete=models.SET_NULL, null=True, blank=True)
    
    description = models.TextField(blank=True, null=True)
    unit = models.CharField(_("Unit"), max_length=20, default="pcs")
    
    base_price = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    def __str__(self):
        return self.name

class ProductVariant(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    sku = models.CharField(_("SKU"), max_length=100, blank=True)
    barcode = models.CharField(_("Barcode"), max_length=100, blank=True, null=True)
    
    cost_price = models.DecimalField(_("Cost (Avg)"), max_digits=12, decimal_places=2, default=0.00)
    sell_price = models.DecimalField(_("Sell Price"), max_digits=12, decimal_places=2, default=0.00)
    
    def __str__(self):
        return f"{self.product.name} ({self.sku})"

    def save(self, *args, **kwargs):
        # Auto-Generate SKU: SupplierPrefix + 3 digits (e.g., 13001)
        if not self.sku and self.product.supplier:
            prefix = self.product.supplier.code_prefix
            last_variant = ProductVariant.objects.filter(
                product__store=self.product.store, 
                sku__startswith=prefix
            ).order_by('sku').last()
            
            if last_variant and last_variant.sku[2:].isdigit():
                next_num = int(last_variant.sku[2:]) + 1
            else:
                next_num = 1
                
            self.sku = f"{prefix}{next_num:03d}"
            
        elif not self.sku:
            self.sku = str(uuid.uuid4())[:8].upper()
            
        super().save(*args, **kwargs)

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

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Update the actual StockLevel
        stock, created = StockLevel.objects.get_or_create(variant=self.variant, branch=self.branch)
        stock.quantity += self.quantity_change
        stock.save()