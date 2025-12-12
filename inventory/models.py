import uuid
from django.db import models
from django.db.models import IntegerField
from django.db.models.functions import Cast
from django.utils.translation import gettext_lazy as _
from core.models import TimestampedModel, Store

class Supplier(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='suppliers')
    name = models.CharField(_("Supplier Name"), max_length=255)
    contact_info = models.TextField(_("Contact Info"), blank=True, null=True)
    code_prefix = models.CharField(_("Supplier Code Prefix"), max_length=2, help_text=_("A unique 2-digit number for this supplier."))
    
    class Meta:
        unique_together = [('store', 'name'), ('store', 'code_prefix')]

    def __str__(self):
        return self.name

class AttributeDefinition(TimestampedModel):
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
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('store', 'key')
        ordering = ['created_at']

    def save(self, *args, **kwargs):
        if not self.key:
            from django.utils.text import slugify
            self.key = f"attr_{slugify(self.name)}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class Category(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(_("Category Name"), max_length=150)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='subcategories')
    
    class Meta:
        unique_together = ('store', 'name')
    
    def __str__(self):
        if self.parent:
            return f"{self.parent.name} > {self.name}"
        return self.name

class Product(TimestampedModel):
    class ProductStatus(models.TextChoices):
        DRAFT = 'DRAFT', _('Draft')
        PUBLISHED = 'PUBLISHED', _('Published')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='products')
    name = models.CharField(_("Product Name"), max_length=255)
    product_code = models.CharField(max_length=50, blank=True, editable=False)
    
    # CHANGED: Made optional to support Quick Add (Safety Net)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='products', null=True, blank=True)
    
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=10, choices=ProductStatus.choices, default=ProductStatus.DRAFT)
    description = models.TextField(_("Description"), blank=True, null=True)
    wholesale_price = models.DecimalField(_("Wholesale Price"), max_digits=10, decimal_places=2, default=0.00)
    price = models.DecimalField(_("Retail Price"), max_digits=10, decimal_places=2, default=0.00)
    stock_quantity = models.IntegerField(_("In Stock"), default=0)

    class Meta:
        unique_together = ('store', 'product_code')
        ordering = ['name']

    def __str__(self):
        return f"{self.name} [{self.product_code}]"

    @property
    def profit(self):
        return self.price - self.wholesale_price

    def save(self, *args, **kwargs):
        # Auto-generate Product Code
        if not self.product_code:
            target_supplier = self.supplier or self.store.default_supplier
            
            # FIXED: Fallback to 'GEN' if no supplier exists
            if target_supplier:
                prefix = target_supplier.code_prefix
            else:
                prefix = "GEN"

            queryset = Product.objects.filter(store=self.store, product_code__startswith=prefix)
            if queryset.exists():
                max_code = queryset.annotate(
                    code_num=Cast(models.Func(models.F('product_code'), models.Value(len(prefix) + 1), function='SUBSTRING'), output_field=IntegerField())
                ).order_by('-code_num').first()
                
                try:
                    # Safe extraction of number
                    code_str = max_code.product_code[len(prefix):]
                    if code_str.isdigit():
                        last_number = int(code_str)
                        new_number = last_number + 1
                    else:
                        new_number = 1
                except (ValueError, AttributeError, IndexError):
                    new_number = 1
            else:
                new_number = 1
            
            self.product_code = f"{prefix}{new_number:03d}"
            
        super().save(*args, **kwargs)

class ProductAttribute(models.Model):
    """Links a Product to a specific Attribute Definition and stores the value."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='attributes')
    definition = models.ForeignKey(AttributeDefinition, on_delete=models.PROTECT)
    value = models.CharField(max_length=255)

    class Meta:
        unique_together = ('product', 'definition')

    def __str__(self):
        return f"{self.definition.name}: {self.value}"