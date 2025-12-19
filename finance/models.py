import uuid
from decimal import Decimal
from django.db import models
from django.db.models import Sum, Max
from django.utils.translation import gettext_lazy as _
from core.models import TimestampedModel, SoftDeleteModel, Store, Branch
from inventory.models import ProductVariant
from users.models import Customer

# --- 1. SEQUENCING ---
class InvoiceSequence(models.Model):
    """Tracks the last invoice number for each store."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    last_number = models.PositiveIntegerField(default=0)
    
    class Meta:
        unique_together = ('store',)

# --- 2. EXPENSES ---
class ExpenseCategory(TimestampedModel, SoftDeleteModel):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='expense_categories')
    name = models.CharField(max_length=100)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return self.name

class Expense(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='expenses')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.TextField(blank=True)
    date = models.DateField()

# --- 3. INVOICING ---
class PaymentMethod(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='payment_methods')
    name = models.CharField(max_length=100)
    is_cash = models.BooleanField(default=False) # To identify Cash Drawer
    
    def __str__(self):
        return self.name

class SalesInvoice(TimestampedModel, SoftDeleteModel):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', _('Draft')
        POSTED = 'POSTED', _('Posted') # Finalized
        VOID = 'VOID', _('Voided')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='invoices')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='invoices')
    
    # The Readable Number (e.g., 1001)
    invoice_number = models.PositiveIntegerField(editable=False, null=True)
    
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    date = models.DateTimeField()
    
    # Totals
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    
    def save(self, *args, **kwargs):
        # Auto-Sequence on first save if POSTED
        if self.status == self.Status.POSTED and not self.invoice_number:
            seq, created = InvoiceSequence.objects.get_or_create(store=self.store)
            seq.last_number += 1
            seq.save()
            self.invoice_number = seq.last_number
        super().save(*args, **kwargs)

class SalesInvoiceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total = models.DecimalField(max_digits=12, decimal_places=2)

    def save(self, *args, **kwargs):
        self.total = (self.quantity * self.unit_price) + self.tax_amount
        super().save(*args, **kwargs)

class Payment(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name='payments')
    method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Update Invoice Paid Amount
        total_paid = self.invoice.payments.aggregate(sum=Sum('amount'))['sum'] or 0
        self.invoice.paid_amount = total_paid
        self.invoice.save()