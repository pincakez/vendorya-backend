import uuid
from decimal import Decimal
from django.db import models, transaction
from django.db.models import Sum, Max
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.db.models.signals import pre_save
from django.dispatch import receiver
from core.models import TimestampedModel, SoftDeleteModel, Store, Branch
from core.tenancy import TenantScopedManager, TenantSoftDeleteManager
# ADDED StockLevel here
from inventory.models import (
    ProductVariant, ProductUnit, StockLevel,
    is_expiry_tracked, draw_from_batches, restock_to_batch,
)
from users.models import Customer

# --- 1. SEQUENCING ---
class InvoiceSequence(models.Model):
    """Tracks the last invoice number for each store."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('store',)


class PurchaseSequence(models.Model):
    """Tracks the last internal purchase number per (store, supplier).

    Purchase numbers are human-readable per supplier: the supplier's 3-digit
    code_prefix followed by a running 2-digit (min) counter — e.g. 40001, 40002.
    """
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    supplier = models.ForeignKey('inventory.Supplier', on_delete=models.CASCADE)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('store', 'supplier')

# --- 2. EXPENSES ---
class ExpenseCategory(TimestampedModel, SoftDeleteModel):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='expense_categories')
    name = models.CharField(max_length=100)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL)

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

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

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

# --- 3. INVOICING ---
class PaymentMethod(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='payment_methods')
    name = models.CharField(max_length=100)
    is_cash = models.BooleanField(default=False)
    is_agel = models.BooleanField(default=False, help_text="Agel (credit) sales only. Enforces credit limit and tracks customer debt.")

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

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

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    def save(self, *args, **kwargs):
        if self.status == self.Status.POSTED and not self.invoice_number:
            with transaction.atomic():
                seq = InvoiceSequence.objects.select_for_update().get_or_create(store=self.store)[0]
                seq.last_number += 1
                seq.save()
                self.invoice_number = seq.last_number
        super().save(*args, **kwargs)

class SalesInvoiceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)

    # Opt-in unit of measure. NULL = the variant's base unit (factor 1). When a
    # selling unit is chosen (e.g. a Strip = 10 tablets), unit_factor is frozen
    # here at sale time so later edits to the unit never rewrite this invoice.
    unit = models.ForeignKey(ProductUnit, on_delete=models.PROTECT, null=True, blank=True)
    unit_factor = models.DecimalField(max_digits=12, decimal_places=3, default=1)

    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total = models.DecimalField(max_digits=12, decimal_places=2)

    # COGS snapshot — captured from the weighted-average received purchase cost at the
    # moment the invoice is POSTED. Reports use THIS, never ProductVariant.cost_price.
    cost_at_sale = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    def save(self, *args, **kwargs):
        discount = Decimal(str(self.discount_amount or '0'))
        tax = Decimal(str(self.tax_amount or '0'))
        self.total = (self.quantity * self.unit_price) - discount + tax
        super().save(*args, **kwargs)

class SaleBatchConsumption(models.Model):
    """Audit ledger: which batches a POSTED sale line drew from, and how much (base units).

    Written by the FEFO draw in handle_sale_stock. Two jobs: (1) true-FEFO COGS — the
    line's cost is the sum of (base_qty × cost_per_base) here, not a weighted average;
    (2) exact reversal — a VOID returns each draw to its origin batch, so expiry dates
    never have to be guessed. cost_per_base is frozen at draw time.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(SalesInvoiceItem, on_delete=models.CASCADE, related_name='batch_consumptions')
    batch = models.ForeignKey('inventory.StockBatch', on_delete=models.PROTECT, related_name='consumptions')
    base_qty = models.DecimalField(max_digits=12, decimal_places=3)
    cost_per_base = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)


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

    objects     = TenantScopedManager()   # secure-by-default
    all_objects = models.Manager()        # unscoped escape hatch (sudo/audit/commands)

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

    # Returns policy fields (s41). restocking_fee is deducted from the gross
    # total_refunded to get the net payout. refund_method records how the payout
    # was given; STORE_CREDIT accrues to the customer's store_credit wallet.
    restocking_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    class RefundMethod(models.TextChoices):
        CASH = 'CASH', _('Cash / Original')
        STORE_CREDIT = 'STORE_CREDIT', _('Store Credit')

    refund_method = models.CharField(
        max_length=15, choices=RefundMethod.choices, default=RefundMethod.CASH)

    # Who processed the return — used for per-cashier returns in reports.
    # Existing rows stay null ("unattributed").
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name='refunds_created',
    )

    reason = models.TextField(blank=True)

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    @property
    def net_refund(self):
        """What the customer actually receives = gross items total − restocking fee."""
        return (self.total_refunded or Decimal('0')) - (self.restocking_fee or Decimal('0'))

    def save(self, *args, **kwargs):
        if not self.refund_number:
            # all_objects: numbering must be correct even outside a request
            # context (e.g. background jobs) where tenant scope isn't armed.
            # Lock this store's existing refunds so concurrent inserts serialize.
            with transaction.atomic():
                list(RefundInvoice.all_objects.select_for_update()
                     .filter(store=self.store).values_list('id', flat=True))
                last = (RefundInvoice.all_objects.filter(store=self.store)
                        .aggregate(Max('refund_number'))['refund_number__max']) or 0
                self.refund_number = last + 1
                super().save(*args, **kwargs)
                return
        super().save(*args, **kwargs)

class RefundItem(models.Model):
    refund = models.ForeignKey(RefundInvoice, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    # unit_factor frozen from the original sold unit (default 1 = base unit), so
    # restock returns the correct number of base units.
    unit_factor = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2)

    restock_inventory = models.BooleanField(default=True, help_text="If True, adds item back to stock.")

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new and self.restock_inventory:
            base_qty = Decimal(str(self.quantity)) * Decimal(str(self.unit_factor or 1))
            with transaction.atomic():
                stock, _ = StockLevel.objects.select_for_update().get_or_create(
                    variant=self.variant, branch=self.refund.branch
                )
                stock.quantity += base_qty
                stock.save()
                # Returned goods re-enter batch stock for tracked products. Origin
                # batch is unknown from a refund line, so they land in an
                # unknown-expiry batch (FEFO sorts those last; staff can adjust).
                if is_expiry_tracked(self.variant):
                    restock_to_batch(
                        self.variant, self.refund.branch, self.refund.store, base_qty,
                        expiry_date=None, batch_number='')
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
    supplier = models.ForeignKey('inventory.Supplier', on_delete=models.PROTECT, related_name='purchases', null=True, blank=True)
    
    purchase_number = models.CharField(
        _("Purchase #"), max_length=20, blank=True, default='',
        help_text="Our internal number (supplier prefix + running counter). Auto-filled when a supplier is set and left blank.")
    vendor_reference = models.CharField(_("Supplier Invoice #"), max_length=100, blank=True, help_text="The number on the paper invoice they gave you.")
    date = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)

    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    notes = models.TextField(blank=True)

    # Staged NEW-product lines that have no supplier yet (so no SKU). Each entry:
    # {name, category, attributes:[{definition, value}], quantity, base_price, retail_price}.
    # Materialized into real Product + ProductVariant + PurchaseItem the moment a
    # supplier is assigned (see PurchaseInvoiceSerializer).
    draft_items = models.JSONField(default=list, blank=True)

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    def save(self, *args, **kwargs):
        # Auto-assign our internal purchase number once a supplier exists and the
        # field is still blank. Format: supplier.code_prefix + zero-padded counter.
        if self.supplier_id and not self.purchase_number:
            with transaction.atomic():
                seq, _created = PurchaseSequence.objects.select_for_update().get_or_create(
                    store=self.store, supplier_id=self.supplier_id,
                )
                seq.last_number += 1
                seq.save()
                self.purchase_number = f"{self.supplier.code_prefix}{seq.last_number:02d}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.supplier.name} - {self.vendor_reference or self.id}"

class PurchaseItem(models.Model):
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    # Opt-in unit. NULL = base unit. unit_factor frozen at receive time; quantity
    # and unit_cost are expressed in the chosen unit (e.g. 10 packs @ pack cost).
    unit = models.ForeignKey(ProductUnit, on_delete=models.PROTECT, null=True, blank=True)
    unit_factor = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, help_text="Cost per item in this specific shipment.")
    total_cost = models.DecimalField(max_digits=12, decimal_places=2)

    # Expiry-tracked products only: the lot delivered on this line. Captured at receive
    # time and used to create the StockBatch (see handle_purchase_stock). Ignored when
    # the product isn't expiry-tracked.
    expiry_date = models.DateField(null=True, blank=True)
    batch_number = models.CharField(max_length=60, blank=True, default='')

    def save(self, *args, **kwargs):
        self.total_cost = self.quantity * self.unit_cost
        super().save(*args, **kwargs)
        
        # Update Invoice Total
        total = self.invoice.items.aggregate(sum=Sum('total_cost'))['sum'] or 0
        self.invoice.total_amount = total
        self.invoice.save()

def weighted_avg_cost(variant, store):
    """Weighted-average unit cost from RECEIVED purchases of this variant in this store.

    COGS source of truth: actual purchase-invoice prices, NOT ProductVariant.cost_price.
    Falls back to the variant's stored cost_price, then 0, when no purchases exist.
    """
    from django.db.models import F
    agg = PurchaseItem.objects.filter(
        variant=variant,
        invoice__store=store,
        invoice__status=PurchaseInvoice.Status.RECEIVED,
        invoice__is_deleted=False,
    ).aggregate(
        total_cost=Sum('total_cost'),
        # total_cost is the money paid; quantity must be converted to BASE units
        # (qty × unit_factor) so the average is per base unit, not per purchase unit.
        total_qty=Sum(F('quantity') * F('unit_factor')),
    )
    total_cost = agg['total_cost'] or Decimal('0')
    total_qty = agg['total_qty'] or Decimal('0')
    if total_qty > 0:
        return (total_cost / total_qty).quantize(Decimal('0.01'))
    return variant.cost_price or Decimal('0')


def customer_outstanding(customer, exclude_invoice_id=None):
    """A customer's REAL balance = opening-balance seed + Σ over their POSTED,
    non-deleted invoices of (grand_total − paid_amount − refunded).

    This is the single source of truth — for credit-limit enforcement, the
    Customers list/detail display, and V-Pilot. The stored `Customer.balance`
    column is the *opening-balance seed* only (a manual starting figure entered
    at onboarding, e.g. via V-Pilot `create_customer(opening_balance=…)`); live
    invoice activity is never written back to it, so the running total must be
    computed here (Option A, s31; opening seed folded in s61). Mirrors the
    AR-aging report. Positive = they owe us; negative = we owe them.
    """
    from django.db.models import Q, Value, DecimalField
    from django.db.models.functions import Coalesce
    DEC = DecimalField(max_digits=14, decimal_places=2)
    ZERO = Decimal('0')
    qs = SalesInvoice.all_objects.filter(
        customer=customer, status=SalesInvoice.Status.POSTED, is_deleted=False,
    )
    if exclude_invoice_id:
        qs = qs.exclude(id=exclude_invoice_id)
    qs = qs.annotate(refunded=Coalesce(
        Sum('refunds__total_refunded', filter=Q(refunds__is_deleted=False)),
        Value(ZERO), output_field=DEC))
    total = customer.balance or ZERO   # opening-balance seed
    for inv in qs:
        total += (inv.grand_total or ZERO) - (inv.paid_amount or ZERO) - (inv.refunded or ZERO)
    return total


@receiver(pre_save, sender=SalesInvoice)
def handle_sale_stock(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = SalesInvoice.objects.get(pk=instance.pk)
            if old.status == SalesInvoice.Status.DRAFT and instance.status == SalesInvoice.Status.POSTED:
                with transaction.atomic():
                    for item in instance.items.all():
                        # Stock lives in base units; convert the sold quantity
                        # (which may be in Strips/Packs) down to base units.
                        base_qty = Decimal(str(item.quantity)) * Decimal(str(item.unit_factor or 1))
                        stock = StockLevel.objects.select_for_update().filter(
                            variant=item.variant, branch=instance.branch
                        ).first()
                        if stock:
                            stock.quantity -= base_qty
                            stock.save()
                            # Fire low-stock notification once per variant crossing
                            # its own reorder_level threshold.
                            if stock.quantity <= (item.variant.reorder_level or 5):
                                from notifications.dispatcher import send_notification
                                from notifications.models import Notification as Notif
                                send_notification(
                                    store=instance.store,
                                    title=f"Low stock: {item.variant.product.name} ({item.variant.sku})",
                                    body=f"Only {stock.quantity} left at {instance.branch.name}",
                                    priority=Notif.Priority.WARNING,
                                    notif_type=Notif.Type.LOW_STOCK,
                                    link="/inventory/products",
                                )
                        if is_expiry_tracked(item.variant):
                            # FEFO: draw the base qty from the earliest-expiry batches,
                            # record each draw (for VOID + true costing), and snapshot
                            # COGS as the ACTUAL cost of the batches consumed.
                            draws = draw_from_batches(item.variant, instance.branch, base_qty)
                            total_base_cost = Decimal('0')
                            for d in draws:
                                SaleBatchConsumption.objects.create(
                                    item=item, batch=d['batch'],
                                    base_qty=d['qty'], cost_per_base=d['cost_per_base'])
                                total_base_cost += d['qty'] * d['cost_per_base']
                            # cost_at_sale is per SOLD unit: total batch cost ÷ sold qty.
                            qty = Decimal(str(item.quantity)) or Decimal('1')
                            item.cost_at_sale = (total_base_cost / qty).quantize(Decimal('0.01'))
                            item.save(update_fields=['cost_at_sale'])
                        else:
                            # Snapshot COGS at the moment of posting. weighted_avg_cost
                            # is per BASE unit; scale to the sold unit so the line's
                            # profit math (unit_price − cost_at_sale) × quantity stays
                            # correct when selling Strips/Packs.
                            base_cost = weighted_avg_cost(item.variant, instance.store)
                            item.cost_at_sale = (base_cost * Decimal(str(item.unit_factor or 1))).quantize(Decimal('0.01'))
                            item.save(update_fields=['cost_at_sale'])
            elif old.status == SalesInvoice.Status.POSTED and instance.status == SalesInvoice.Status.VOID:
                # Reversing a posted sale: put the stock back so inventory stays
                # accurate. Mirrors the DRAFT→POSTED decrement above. (DRAFT→VOID
                # never decremented, so it must NOT add stock.)
                with transaction.atomic():
                    for item in instance.items.all():
                        base_qty = Decimal(str(item.quantity)) * Decimal(str(item.unit_factor or 1))
                        stock = StockLevel.objects.select_for_update().filter(
                            variant=item.variant, branch=instance.branch
                        ).first()
                        if stock:
                            stock.quantity += base_qty
                            stock.save()
                        # Return each FEFO draw to its exact origin batch, then clear
                        # the consumption rows so the void can't double-reverse.
                        if is_expiry_tracked(item.variant):
                            for c in item.batch_consumptions.select_for_update():
                                restock_to_batch(
                                    item.variant, instance.branch, instance.store,
                                    c.base_qty, prefer_batch=c.batch)
                                c.delete()
        except SalesInvoice.DoesNotExist:
            pass


@receiver(pre_save, sender=PurchaseInvoice)
def handle_purchase_stock(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = PurchaseInvoice.objects.get(pk=instance.pk)
            if old.status == PurchaseInvoice.Status.DRAFT and instance.status == PurchaseInvoice.Status.RECEIVED:
                with transaction.atomic():
                    for item in instance.items.all():
                        factor = Decimal(str(item.unit_factor or 1))
                        base_qty = Decimal(str(item.quantity)) * factor
                        stock, _ = StockLevel.objects.select_for_update().get_or_create(
                            variant=item.variant, branch=instance.branch
                        )
                        stock.quantity = Decimal(str(stock.quantity)) + base_qty
                        stock.save()
                        # cost_price is per BASE unit; unit_cost is per purchased
                        # unit (e.g. a pack), so divide by the factor.
                        base_cost = (Decimal(str(item.unit_cost)) / factor).quantize(Decimal('0.01'))
                        item.variant.cost_price = base_cost
                        item.variant.save()
                        # Expiry-tracked: this delivery becomes a dated batch, counted
                        # in base units, carrying its own true cost for FEFO COGS.
                        if is_expiry_tracked(item.variant):
                            restock_to_batch(
                                item.variant, instance.branch, instance.store, base_qty,
                                expiry_date=item.expiry_date,
                                batch_number=item.batch_number,
                                cost_per_base=base_cost,
                                source_purchase_item=item,
                            )
        except PurchaseInvoice.DoesNotExist:
            pass