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

class Category(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(_("Category Name"), max_length=150)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='subcategories')
    
    class Meta:
        unique_together = ('store', 'name')
    
    def __str__(self):
        return self.name

class Product(TimestampedModel):
    class ProductStatus(models.TextChoices):
        DRAFT = 'DRAFT', _('Draft')
        PUBLISHED = 'PUBLISHED', _('Published')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='products')
    name = models.CharField(_("Product Name"), max_length=255)
    product_code = models.CharField(max_length=50, blank=True, editable=False)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='products')
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=10, choices=ProductStatus.choices, default=ProductStatus.DRAFT)
    description = models.TextField(_("Description"), blank=True, null=True)
    
    # Simple pricing fields for now (can be expanded later)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    stock_quantity = models.IntegerField(default=0)

    class Meta:
        unique_together = ('store', 'product_code')
        ordering = ['name']

    def __str__(self):
        return f"{self.name} [{self.product_code}]" if self.product_code else self.name

    def save(self, *args, **kwargs):
        # Auto-generate product code logic
        if not self.product_code:
            target_supplier = self.supplier or self.store.default_supplier
            if target_supplier:
                prefix = target_supplier.code_prefix
                queryset = Product.objects.filter(store=self.store, product_code__startswith=prefix)
                if queryset.exists():
                    # Extract number from code (e.g., "AA005" -> 5)
                    max_code = queryset.annotate(
                        code_num=Cast(models.Func(models.F('product_code'), models.Value(len(prefix) + 1), function='SUBSTRING'), output_field=IntegerField())
                    ).order_by('-code_num').first()
                    
                    try:
                        last_number = int(max_code.product_code[len(prefix):])
                        new_number = last_number + 1
                    except (ValueError, AttributeError):
                        new_number = 1
                else:
                    new_number = 1
                self.product_code = f"{prefix}{new_number:03d}"
        super().save(*args, **kwargs)