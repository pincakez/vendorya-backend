import uuid
from decimal import Decimal
from django.db import models
from django.db.models import Sum
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from core.models import TimestampedModel, Store
from inventory.models import Product
from users.models import Customer

class PaymentMethod(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='payment_methods')
    name = models.CharField(max_length=100)
    class Meta:
        unique_together = ('store', 'name')
    def __str__(self):
        return self.name

class BaseInvoice(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice_date = models.DateField()
    subtotal_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, editable=False)
    shipping_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, editable=False)
    class Meta:
        abstract = True

class BaseInvoiceItem(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # We link to Product directly for now (simplified from ProductVariant)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True)
    description = models.CharField(max_length=255, default='')
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    total_price = models.DecimalField(max_digits=12, decimal_places=2, editable=False)
    class Meta:
        abstract = True
    def save(self, *args, **kwargs):
        self.total_price = self.quantity * self.unit_price
        super().save(*args, **kwargs)

class SalesInvoice(BaseInvoice):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='sales_invoices')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='sales_invoices')

class SalesInvoiceItem(BaseInvoiceItem):
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name='items')

# Signals for automatic calculation
@receiver([post_save, post_delete], sender=SalesInvoiceItem)
def update_invoice_totals(sender, instance, **kwargs):
    invoice = instance.invoice
    subtotal = invoice.items.aggregate(total=Sum('total_price'))['total'] or Decimal('0.00')
    invoice.subtotal_amount = subtotal
    invoice.total_amount = (invoice.subtotal_amount + invoice.shipping_fee) - invoice.discount_amount
    invoice.save(update_fields=['subtotal_amount', 'total_amount'])