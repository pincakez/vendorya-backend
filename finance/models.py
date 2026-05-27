import uuid
from decimal import Decimal
from django.db import models
from django.db.models import Sum, Max
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.db.models.signals import pre_save
from django.dispatch import receiver
from core.models import TimestampedModel, SoftDeleteModel, Store, Branch
# ADDED StockLevel here
from inventory.models import ProductVariant, StockLevel
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
    
    # Added to track WHO took the money (for Shifts)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Update Invoice Paid Amount
        total_paid = self.invoice.payments.aggregate(sum=Sum('amount'))['sum'] or 0
        self.invoice.paid_amount = total_paid
        self.invoice.save()

# --- 4. SHIFT MANAGEMENT ---
class WorkShift(TimestampedModel):
    """Tracks a cashier's session (Open/Close)."""
    class Status(models.TextChoices):
        OPEN = 'OPEN', _('Open')
        CLOSED = 'CLOSED', _('Closed')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='shifts')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='shifts')
    
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    
    # Money Tracking
    starting_cash = models.DecimalField(_("Starting Cash"), max_digits=12, decimal_places=2, default=0.00)
    closing_cash = models.DecimalField(_("Closing Cash (Counted)"), max_digits=12, decimal_places=2, default=0.00)
    
    # System Calculated (Read Only)
    expected_cash = models.DecimalField(_("Expected Cash"), max_digits=12, decimal_places=2, default=0.00)
    difference = models.DecimalField(_("Difference"), max_digits=12, decimal_places=2, default=0.00)
    
    def __str__(self):
        return f"{self.user.username} - {self.start_time.date()}"

    def close_shift(self, counted_cash):
        """Closes the shift and calculates shortage/overage."""
        from django.utils import timezone
        
        # 1. Calculate Expected Cash
        cash_sales = Payment.objects.filter(
            invoice__store=self.store,
            created_at__gte=self.start_time,
            method__is_cash=True,
            created_by=self.user
        ).aggregate(sum=Sum('amount'))['sum'] or 0
        
        self.expected_cash = self.starting_cash + cash_sales
        self.closing_cash = counted_cash
        self.difference = self.closing_cash - self.expected_cash
        
        self.end_time = timezone.now()
        self.status = self.Status.CLOSED
        self.save()

# --- 5. REFUNDS ---
class RefundInvoice(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='refunds')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)
    
    original_invoice = models.ForeignKey(SalesInvoice, on_delete=models.SET_NULL, null=True, blank=True, related_name='refunds')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    
    refund_number = models.PositiveIntegerField(editable=False, null=True)
    date = models.DateTimeField(auto_now_add=True)
    total_refunded = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    
    reason = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.refund_number:
            last = RefundInvoice.objects.filter(store=self.store).aggregate(Max('refund_number'))['refund_number__max'] or 0
            self.refund_number = last + 1
        super().save(*args, **kwargs)

class RefundItem(models.Model):
    refund = models.ForeignKey(RefundInvoice, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2)
    
    restock_inventory = models.BooleanField(default=True, help_text="If True, adds item back to stock.")

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        total = self.refund.items.aggregate(sum=Sum('refund_amount'))['sum'] or 0
        self.refund.total_refunded = total
        self.refund.save()

# --- 6. PURCHASING ---
class PurchaseInvoice(TimestampedModel, SoftDeleteModel):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', _('Draft')
        RECEIVED = 'RECEIVED', _('Received') # Stock Added

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='purchases')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)
    supplier = models.ForeignKey('inventory.Supplier', on_delete=models.PROTECT, related_name='purchases')
    
    vendor_reference = models.CharField(_("Supplier Invoice #"), max_length=100, blank=True, help_text="The number on the paper invoice they gave you.")
    date = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.supplier.name} - {self.vendor_reference or self.id}"

class PurchaseItem(models.Model):
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, help_text="Cost per item in this specific shipment.")
    total_cost = models.DecimalField(max_digits=12, decimal_places=2)

    def save(self, *args, **kwargs):
        self.total_cost = self.quantity * self.unit_cost
        super().save(*args, **kwargs)
        
        # Update Invoice Total
        total = self.invoice.items.aggregate(sum=Sum('total_cost'))['sum'] or 0
        self.invoice.total_amount = total
        self.invoice.save()

@receiver(pre_save, sender=PurchaseInvoice)
def handle_purchase_stock(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = PurchaseInvoice.objects.get(pk=instance.pk)
            # If changing from DRAFT to RECEIVED -> Add Stock
            if old.status == PurchaseInvoice.Status.DRAFT and instance.status == PurchaseInvoice.Status.RECEIVED:
                for item in instance.items.all():
                    # 1. Increase Stock
                    stock, created = StockLevel.objects.get_or_create(variant=item.variant, branch=instance.branch)
                    stock.quantity += item.quantity
                    stock.save()
                    
                    # 2. Update Product Cost
                    item.variant.cost_price = item.unit_cost
                    item.variant.save()
        except PurchaseInvoice.DoesNotExist:
            pass