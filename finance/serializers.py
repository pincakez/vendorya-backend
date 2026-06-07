from decimal import Decimal
from rest_framework import serializers
from django.db import transaction
from .models import (
    SalesInvoice, SalesInvoiceItem, Payment, PaymentMethod,
    PurchaseInvoice, PurchaseItem,
    Expense, ExpenseCategory,
    WorkShift,
    RefundInvoice, RefundItem,
    customer_outstanding,
)


def enforce_credit_policy(invoice):
    """Apply the store's credit policy (ALLOW / WARN / BLOCK) to a sale whose
    unpaid balance would push the customer past their credit limit.

    Outstanding is computed LIVE from the customer's posted invoices
    (`customer_outstanding`), since `Customer.balance` is not maintained.
    Call this at the moment credit is actually extended — i.e. when posting
    (checkout), not when a draft cart is created. Raises ValidationError on BLOCK.
    """
    unpaid = invoice.grand_total - invoice.paid_amount
    if unpaid <= 0:
        return  # fully paid — no credit involved
    customer = invoice.customer
    store = invoice.store
    settings = getattr(store, 'settings', None)
    if settings is None:
        return
    effective_limit = customer.credit_limit
    if effective_limit is None:
        effective_limit = settings.default_credit_limit
    if effective_limit is None:
        return  # no limit configured

    new_balance = customer_outstanding(customer, exclude_invoice_id=invoice.id) + unpaid
    if new_balance <= effective_limit:
        return

    policy = settings.credit_policy
    if policy == 'BLOCK':
        raise serializers.ValidationError(
            f"Credit limit exceeded for {customer.name}. "
            f"Limit: {effective_limit}, would-be balance: {new_balance}."
        )
    if policy == 'WARN':
        from notifications.dispatcher import send_notification
        from notifications.models import Notification
        send_notification(
            store=store,
            title=f"Credit limit exceeded: {customer.name}",
            body=(f"Invoice #{invoice.invoice_number or '(draft)'} — unpaid {unpaid}. "
                  f"New balance {new_balance} exceeds limit {effective_limit}."),
            priority=Notification.Priority.WARNING,
            link="/people/customers",
        )


class PaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentMethod
        fields = ['id', 'name', 'is_cash', 'is_agel']
        read_only_fields = ['id']


# --- SALES ---

class SalesInvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalesInvoiceItem
        fields = ['id', 'variant', 'quantity', 'unit_price', 'discount_amount', 'tax_amount', 'total']
        read_only_fields = ['id', 'tax_amount', 'total']


class SalesInvoiceSerializer(serializers.ModelSerializer):
    items = SalesInvoiceItemSerializer(many=True, required=False)

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'branch', 'customer', 'invoice_number', 'status', 'date',
            'subtotal', 'tax_total', 'discount', 'grand_total', 'paid_amount',
            'items', 'created_at',
        ]
        read_only_fields = [
            'id', 'invoice_number', 'subtotal', 'tax_total',
            'grand_total', 'paid_amount', 'created_at',
        ]

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        with transaction.atomic():
            invoice = SalesInvoice.objects.create(**validated_data)
            for item_data in items_data:
                self._apply_line_tax(invoice, item_data)
                SalesInvoiceItem.objects.create(invoice=invoice, **item_data)
            self._recalculate(invoice)
        # Only enforce credit when the invoice is created already-POSTED (legacy
        # direct-post path). The POS flow creates a DRAFT cart then posts via the
        # checkout action, which runs enforce_credit_policy at the real post moment.
        if invoice.status == SalesInvoice.Status.POSTED:
            enforce_credit_policy(invoice)
        return invoice

    @staticmethod
    def _apply_line_tax(invoice, item_data):
        """Server-side tax: set the line's tax_amount from the product's tax
        (or the store default), or 0 when tax is disabled for the store.
        Single source of truth — POS and the invoice screen both rely on this
        instead of trusting a client-supplied tax_amount."""
        settings = getattr(invoice.store, 'settings', None)
        if settings is not None and not getattr(settings, 'tax_enabled', True):
            item_data['tax_amount'] = Decimal('0')
            return
        variant = item_data.get('variant')
        tax = getattr(getattr(variant, 'product', None), 'tax', None)
        if tax is None and settings is not None:
            tax = settings.default_tax
        if tax is None:
            item_data['tax_amount'] = Decimal('0')
            return
        qty = item_data.get('quantity') or Decimal('0')
        unit = item_data.get('unit_price') or Decimal('0')
        item_data['tax_amount'] = (qty * unit * tax.rate / Decimal('100')).quantize(Decimal('0.01'))

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()
            if items_data is not None:
                instance.items.all().delete()
                for item_data in items_data:
                    self._apply_line_tax(instance, item_data)
                    SalesInvoiceItem.objects.create(invoice=instance, **item_data)
            self._recalculate(instance)
        return instance

    @staticmethod
    def _recalculate(invoice):
        subtotal = Decimal('0')
        tax_total = Decimal('0')
        for item in invoice.items.all():
            line_discount = Decimal(str(item.discount_amount or '0'))
            subtotal += (item.quantity * item.unit_price) - line_discount
            tax_total += item.tax_amount
        invoice_discount = Decimal(str(invoice.discount or '0'))
        invoice.subtotal = subtotal
        invoice.tax_total = tax_total
        invoice.grand_total = subtotal + tax_total - invoice_discount
        invoice.save(update_fields=['subtotal', 'tax_total', 'grand_total'])


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ['id', 'invoice', 'method', 'amount', 'created_by', 'created_at']
        read_only_fields = ['id', 'created_by', 'created_at']


# --- PURCHASE ---

class PurchaseItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseItem
        fields = ['id', 'variant', 'quantity', 'unit_cost', 'total_cost']
        read_only_fields = ['id', 'total_cost']


class PurchaseInvoiceSerializer(serializers.ModelSerializer):
    items = PurchaseItemSerializer(many=True, required=False)

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'supplier', 'branch', 'vendor_reference', 'date', 'status',
            'total_amount', 'paid_amount', 'notes', 'items', 'created_at',
        ]
        read_only_fields = ['id', 'total_amount', 'created_at']

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        with transaction.atomic():
            invoice = PurchaseInvoice.objects.create(**validated_data)
            for item_data in items_data:
                PurchaseItem.objects.create(invoice=invoice, **item_data)
        return invoice

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()
            if items_data is not None:
                if instance.status == PurchaseInvoice.Status.RECEIVED:
                    raise serializers.ValidationError(
                        'Cannot modify items on a received purchase.'
                    )
                instance.items.all().delete()
                for item_data in items_data:
                    PurchaseItem.objects.create(invoice=instance, **item_data)
        return instance


# --- EXPENSES ---

class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ['id', 'name', 'parent']
        read_only_fields = ['id']


class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = ['id', 'branch', 'category', 'amount', 'description', 'date', 'created_at']
        read_only_fields = ['id', 'created_at']


# --- SHIFTS ---

class WorkShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkShift
        fields = [
            'id', 'branch', 'user', 'start_time', 'end_time', 'status',
            'starting_cash', 'closing_cash', 'expected_cash', 'difference',
        ]
        read_only_fields = [
            'id', 'user', 'start_time', 'end_time', 'status',
            'expected_cash', 'difference', 'closing_cash',
        ]


# --- REFUNDS ---

class RefundItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = RefundItem
        fields = ['id', 'variant', 'quantity', 'refund_amount', 'restock_inventory']
        read_only_fields = ['id']

    def validate_quantity(self, value):
        # A refund line restocks (+=) inventory; a zero/negative quantity would
        # be meaningless or quietly subtract stock. Refunded qty must be positive.
        if value <= 0:
            raise serializers.ValidationError("Refund quantity must be greater than zero.")
        return value

    def validate_refund_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("Refund amount cannot be negative.")
        return value


class RefundInvoiceSerializer(serializers.ModelSerializer):
    items = RefundItemSerializer(many=True, required=False)

    class Meta:
        model = RefundInvoice
        fields = [
            'id', 'branch', 'original_invoice', 'customer', 'refund_number',
            'date', 'total_refunded', 'reason', 'items',
        ]
        read_only_fields = ['id', 'refund_number', 'date', 'total_refunded']

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        original = validated_data.get('original_invoice')
        with transaction.atomic():
            if original is not None:
                self._validate_against_original(original, items_data)
            refund = RefundInvoice.objects.create(**validated_data)
            for item_data in items_data:
                RefundItem.objects.create(refund=refund, **item_data)
        return refund

    @staticmethod
    def _validate_against_original(original, items_data):
        """Cap each refund line at (sold − already-refunded) for that variant on
        the original invoice, and reject variants that were never sold on it.
        Only enforced when the refund references an original invoice."""
        sold = {}
        for it in original.items.all():
            sold[it.variant_id] = sold.get(it.variant_id, Decimal('0')) + it.quantity
        already = {}
        for ri in RefundItem.objects.filter(
                refund__original_invoice=original, refund__is_deleted=False):
            already[ri.variant_id] = already.get(ri.variant_id, Decimal('0')) + ri.quantity
        requested = {}
        for item in items_data:
            v = item['variant']
            requested[v.id] = requested.get(v.id, Decimal('0')) + item['quantity']

        errors = []
        for vid, req in requested.items():
            sold_q = sold.get(vid, Decimal('0'))
            if sold_q == 0:
                errors.append(f"Variant {vid} was not on the original invoice.")
                continue
            refundable = sold_q - already.get(vid, Decimal('0'))
            if req > refundable:
                errors.append(
                    f"Cannot refund {req} of variant {vid} — only {refundable} "
                    f"refundable (sold {sold_q}, already refunded {already.get(vid, Decimal('0'))})."
                )
        if errors:
            raise serializers.ValidationError({'items': errors})
