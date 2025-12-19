import uuid
from django.db import models
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from core.models import TimestampedModel, SoftDeleteModel, Store, Branch

# --- 1. TAXATION ---
class Tax(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='taxes')
    name = models.CharField(_("Tax Name"), max_length=50) # e.g., VAT 14%
    rate = models.DecimalField(_("Rate %"), max_digits=5, decimal_places=2) # e.g., 14.00
    
    def __str__(self):
        return f"{self.name} ({self.rate}%)"

# --- 2. CORE INVENTORY ---
class Supplier(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='suppliers')
    name = models.CharField(_("Supplier Name"), max_length=255)
    contact_info = models.TextField(_("Contact Info"), blank=True, null=True)
    code_prefix = models.CharField(max_length=5, default="SUP")
    
    def __str__(self):
        return self.name

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
    unit = models.CharField(_("Unit"), max_length=20, default="pcs") # kg, m, box
    
    # Base info (can be overridden by variants)
    base_price = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    def __str__(self):
        return self.name

class ProductVariant(TimestampedModel, SoftDeleteModel):
    """The actual sellable SKU (e.g., Red Shirt Size L)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    sku = models.CharField(_("SKU"), max_length=100, blank=True) # Unique ID
    barcode = models.CharField(_("Barcode"), max_length=100, blank=True, null=True)
    
    # Pricing
    cost_price = models.DecimalField(_("Cost (Avg)"), max_digits=12, decimal_places=2, default=0.00)
    sell_price = models.DecimalField(_("Sell Price"), max_digits=12, decimal_places=2, default=0.00)
    
    def __str__(self):
        return f"{self.product.name} ({self.sku})"

    def save(self, *args, **kwargs):
        if not self.sku:
            # Simple auto-SKU generation
            self.sku = str(uuid.uuid4())[:8].upper()
        super().save(*args, **kwargs)

class ProductAttribute(models.Model):
    """Links a Variant to a specific Attribute (e.g., Variant A is Red)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='attributes')
    definition = models.ForeignKey(AttributeDefinition, on_delete=models.PROTECT)
    value = models.CharField(max_length=255)

    class Meta:
        unique_together = ('variant', 'definition')

# --- 4. MULTI-WAREHOUSE STOCK ---
class StockLevel(TimestampedModel):
    """Tracks how many items are in a specific branch."""
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
    """Defines what is inside a Bundle (e.g., 1 Suit = 1 Jacket + 1 Pants)."""
    bundle = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='bundle_contents')
    component = models.ForeignKey(ProductVariant, on_delete=models.PROTECT) # The part inside
    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=1)