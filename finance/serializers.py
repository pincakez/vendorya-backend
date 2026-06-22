from decimal import Decimal
from rest_framework import serializers
from django.db import transaction
from .models import (
    SalesInvoice, SalesInvoiceItem, Payment, PaymentMethod,
    PurchaseInvoice, PurchaseItem, SupplierPayment,
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

def _stamp_unit_factor(item_data):
    """Freeze unit_factor from the chosen ProductUnit (or 1 for the base unit).
    Mutates and returns item_data so the line records the conversion at sale time.
    Guards against a unit that doesn't belong to this line's variant (would apply
    a wrong factor) by falling back to the base unit."""
    unit = item_data.get('unit')
    variant = item_data.get('variant')
    if unit is not None and variant is not None and unit.variant_id != variant.id:
        unit = None
        item_data['unit'] = None
    item_data['unit_factor'] = unit.factor if unit is not None else Decimal('1')
    return item_data


class SalesInvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalesInvoiceItem
        fields = ['id', 'variant', 'unit', 'unit_factor', 'quantity', 'unit_price',
                  'discount_amount', 'tax_amount', 'total']
        read_only_fields = ['id', 'unit_factor', 'tax_amount', 'total']


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
                SalesInvoiceItem.objects.create(invoice=invoice, **_stamp_unit_factor(item_data))
            self._recalculate(invoice)  # authoritative: sets every line's tax + invoice totals
        # Only enforce credit when the invoice is created already-POSTED (legacy
        # direct-post path). The POS flow creates a DRAFT cart then posts via the
        # checkout action, which runs enforce_credit_policy at the real post moment.
        if invoice.status == SalesInvoice.Status.POSTED:
            enforce_credit_policy(invoice)
        return invoice

    @staticmethod
    def _line_tax_rate(invoice, variant):
        """The tax rate (%) that applies to a line: the product's own tax, else the
        store default, else 0 — and always 0 when tax is disabled for the store."""
        settings = getattr(invoice.store, 'settings', None)
        if settings is not None and not getattr(settings, 'tax_enabled', True):
            return Decimal('0')
        tax = getattr(getattr(variant, 'product', None), 'tax', None)
        if tax is None and settings is not None:
            tax = settings.default_tax
        return tax.rate if tax is not None else Decimal('0')

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()
            if items_data is not None:
                instance.items.all().delete()
                for item_data in items_data:
                    SalesInvoiceItem.objects.create(invoice=instance, **_stamp_unit_factor(item_data))
            self._recalculate(instance)
        return instance

    @staticmethod
    def _recalculate(invoice):
        """Authoritative totals. Tax is charged on the NET price after discounts
        (Egyptian VAT Law 67/2016: VAT base = actual consideration paid; unconditional
        invoice discounts are excluded from the base). So:
          • each line is first netted of its own discount, then
          • the whole-invoice discount is allocated pro-rata across lines, and
          • VAT is computed on what remains.
        """
        items = list(invoice.items.all())
        line_nets = [
            (it.quantity * it.unit_price) - Decimal(str(it.discount_amount or '0'))
            for it in items
        ]
        discounted_subtotal = sum(line_nets, Decimal('0'))

        invoice_discount = Decimal(str(invoice.discount or '0'))
        # never discount below zero
        if invoice_discount < 0:
            invoice_discount = Decimal('0')
        if discounted_subtotal <= 0:
            invoice_discount = Decimal('0')
        elif invoice_discount > discounted_subtotal:
            invoice_discount = discounted_subtotal

        tax_total = Decimal('0')
        for item, line_net in zip(items, line_nets):
            share = (invoice_discount * line_net / discounted_subtotal) if discounted_subtotal > 0 else Decimal('0')
            taxable = line_net - share
            rate = SalesInvoiceSerializer._line_tax_rate(invoice, item.variant)
            tax = (taxable * rate / Decimal('100')).quantize(Decimal('0.01'))
            if Decimal(str(item.tax_amount or '0')) != tax:
                item.tax_amount = tax
                item.save(update_fields=['tax_amount', 'total'])  # save() recomputes total
            tax_total += tax

        invoice.subtotal = discounted_subtotal
        invoice.tax_total = tax_total
        invoice.grand_total = discounted_subtotal - invoice_discount + tax_total
        invoice.save(update_fields=['subtotal', 'tax_total', 'grand_total'])


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ['id', 'invoice', 'method', 'amount', 'created_by', 'created_at']
        read_only_fields = ['id', 'created_by', 'created_at']


# --- PURCHASE ---

class PurchaseItemSerializer(serializers.ModelSerializer):
    track_expiry = serializers.BooleanField(source='variant.product.track_expiry', read_only=True)

    class Meta:
        model = PurchaseItem
        fields = ['id', 'variant', 'unit', 'unit_factor', 'quantity', 'unit_cost', 'total_cost',
                  'expiry_date', 'batch_number', 'track_expiry']
        read_only_fields = ['id', 'unit_factor', 'total_cost']


class PurchaseInvoiceSerializer(serializers.ModelSerializer):
    """Purchase = restock existing products AND/OR onboard brand-new ones.

    WRITE: send `lines` — a flat list of row dicts. Two shapes:
      - existing/materialized:  {variant:<uuid>, quantity, base_price, retail_price?}
      - new product:            {name, category, subcategory?, attributes:[...],
                                 quantity, base_price, retail_price}
    A new-product line materializes into a real Product+Variant+PurchaseItem only
    when the invoice has a supplier (SKU embeds the supplier prefix). With no
    supplier it is STAGED into `draft_items` and materialized later, the moment a
    supplier is assigned.

    READ: `items` = materialized lines (with sku/product_name); `draft_items` =
    staged new-product lines awaiting a supplier.
    """
    items         = serializers.SerializerMethodField()
    lines         = serializers.ListField(child=serializers.DictField(), write_only=True, required=False)
    supplier_name = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'supplier', 'supplier_name', 'branch', 'purchase_number',
            'vendor_reference', 'date', 'status', 'total_amount', 'paid_amount',
            'notes', 'items', 'draft_items', 'lines', 'created_at',
        ]
        # purchase_number is writable: blank → auto-assigned in model.save();
        # a typed value is kept as-is.
        read_only_fields = ['id', 'total_amount', 'created_at',
                            'supplier_name', 'items', 'draft_items']
        extra_kwargs = {
            'supplier': {'required': False, 'allow_null': True},
            'branch':   {'required': False},  # resolved server-side in the view
        }

    def get_supplier_name(self, obj):
        return obj.supplier.name if obj.supplier else None

    def get_items(self, obj):
        from inventory.serializers import build_selling_units
        out = []
        for it in obj.items.select_related('variant__product'):
            v = it.variant
            out.append({
                'id': str(it.id),
                'variant': str(v.id),
                'sku': v.sku,
                'product_name': v.product.name,
                'quantity': str(it.quantity),
                'unit_cost': str(it.unit_cost),
                'retail_price': str(v.sell_price),
                # Round-trip the purchase unit so reopening a draft keeps its
                # pack/strip selection; selling_units feeds the line's unit picker.
                'unit': str(it.unit_id) if it.unit_id else None,
                'selling_units': build_selling_units(v, v.product),
                'expiry_date': it.expiry_date.isoformat() if it.expiry_date else '',
                'batch_number': it.batch_number or '',
                'track_expiry': v.product.track_expiry,
                'kind': 'existing',
            })
        return out

    # ── write helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _dec(val, default='0'):
        try:
            return Decimal(str(val if val not in (None, '') else default))
        except Exception:
            return Decimal(default)

    @staticmethod
    def _resolve_unit(unit_id, variant):
        """Resolve a purchase line's selling unit → (ProductUnit|None, factor).

        NULL/blank → base unit (factor 1). A unit that doesn't belong to this line's
        variant is rejected (falls back to base) so a wrong factor can never be frozen
        onto the PurchaseItem."""
        if not unit_id:
            return None, Decimal('1')
        from inventory.models import ProductUnit
        unit = ProductUnit.objects.filter(id=unit_id, variant=variant).first()
        if unit is None:
            return None, Decimal('1')
        return unit, Decimal(str(unit.factor))

    def _materialize_new(self, invoice, name, category_id, attributes, base, retail,
                         track_expiry=False):
        """Create a real Product+Variant (SKU generated) under the invoice's
        supplier, then a PurchaseItem. Caller guarantees invoice.supplier_id.

        `track_expiry` flags a brand-new product as expiry/batch-tracked on its very
        first receipt (a pharmacy onboarding a new medicine inline). Without it the
        product would default to untracked and the line's expiry_date/batch_number
        would be captured but silently dropped at receive (is_expiry_tracked False)."""
        from inventory.product_service import create_product_with_variant
        from inventory.models import Category
        category = None
        if category_id:
            category = Category.objects.filter(id=category_id, store=invoice.store).first()
        product = create_product_with_variant(
            invoice.store, name=name, supplier=invoice.supplier, category=category,
            base_price=base, cost_price=base, sell_price=retail,
            attributes=attributes or [],
            extra_product_fields={'track_expiry': True} if track_expiry else None,
        )
        return product.variants.first()

    def _apply_lines(self, invoice, lines):
        from inventory.models import ProductVariant
        staged = []
        for line in lines:
            variant_id = line.get('variant')
            name       = (line.get('name') or '').strip()
            if not variant_id and not name:
                continue  # blank trailing row → counts as nothing
            qty  = self._dec(line.get('quantity'), '1')
            if qty <= 0:
                qty = Decimal('1')
            base   = self._dec(line.get('base_price') or line.get('unit_cost'))
            retail = line.get('retail_price')
            # Expiry-tracked lines carry a lot date/number → becomes a StockBatch on
            # receive (ignored by the backend for non-tracked products).
            expiry = line.get('expiry_date') or None
            batch  = (line.get('batch_number') or '').strip()
            track  = bool(line.get('track_expiry'))

            if variant_id:
                # existing product (or a previously-materialized new product)
                variant = ProductVariant.objects.filter(
                    id=variant_id, product__store=invoice.store
                ).first()
                if not variant:
                    continue
                if retail not in (None, ''):
                    variant.sell_price = self._dec(retail)
                    variant.save(update_fields=['sell_price'])
                # Purchase-by-unit: a line may be received in an alternate selling
                # unit (e.g. 10 Packs). Resolve it, guard that it belongs to this
                # variant, and freeze its factor; quantity/unit_cost stay per-unit and
                # the receive engine (handle_purchase_stock) converts to base.
                unit, factor = self._resolve_unit(line.get('unit'), variant)
                PurchaseItem.objects.create(invoice=invoice, variant=variant,
                                            unit=unit, unit_factor=factor,
                                            quantity=qty, unit_cost=base,
                                            expiry_date=expiry, batch_number=batch)
            elif invoice.supplier_id:
                # new product, supplier present → materialize now
                cat_id  = line.get('subcategory') or line.get('category')
                variant = self._materialize_new(
                    invoice, name, cat_id, line.get('attributes'),
                    base, self._dec(retail), track_expiry=track,
                )
                PurchaseItem.objects.create(invoice=invoice, variant=variant,
                                            quantity=qty, unit_cost=base,
                                            expiry_date=expiry, batch_number=batch)
            else:
                # new product, NO supplier → stage it (no SKU, not real inventory)
                staged.append({
                    'name': name,
                    'category': line.get('category'),
                    'subcategory': line.get('subcategory'),
                    'attributes': line.get('attributes') or [],
                    'quantity': str(qty),
                    'base_price': str(base),
                    'retail_price': str(self._dec(retail)),
                    'track_expiry': track,
                    'expiry_date': expiry.isoformat() if hasattr(expiry, 'isoformat') else (expiry or None),
                    'batch_number': batch,
                })
        invoice.draft_items = staged
        invoice.save(update_fields=['draft_items'])

    def _materialize_staged(self, invoice):
        """Supplier just assigned → turn every staged draft line into a real
        Product+Variant+PurchaseItem, then clear the staging area."""
        for line in (invoice.draft_items or []):
            cat_id  = line.get('subcategory') or line.get('category')
            base    = self._dec(line.get('base_price'))
            variant = self._materialize_new(
                invoice, line.get('name') or 'Unnamed', cat_id,
                line.get('attributes'), base, self._dec(line.get('retail_price')),
                track_expiry=bool(line.get('track_expiry')),
            )
            PurchaseItem.objects.create(
                invoice=invoice, variant=variant,
                quantity=self._dec(line.get('quantity'), '1'), unit_cost=base,
                expiry_date=line.get('expiry_date') or None,
                batch_number=(line.get('batch_number') or '').strip(),
            )
        invoice.draft_items = []
        invoice.save(update_fields=['draft_items'])

    def create(self, validated_data):
        lines = validated_data.pop('lines', [])
        with transaction.atomic():
            invoice = PurchaseInvoice.objects.create(**validated_data)
            self._apply_lines(invoice, lines)
        return invoice

    def update(self, instance, validated_data):
        lines = validated_data.pop('lines', None)
        with transaction.atomic():
            had_supplier = bool(instance.supplier_id)
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()  # may auto-assign purchase_number now
            if instance.status == PurchaseInvoice.Status.RECEIVED and lines is not None:
                raise serializers.ValidationError(
                    'Cannot modify items on a received purchase.'
                )
            if lines is not None:
                instance.items.all().delete()
                self._apply_lines(instance, lines)
            elif not had_supplier and instance.supplier_id and instance.draft_items:
                # supplier assigned with no new rows → materialize the staged ones
                self._materialize_staged(instance)
        return instance


class SupplierPaymentSerializer(serializers.ModelSerializer):
    method_display = serializers.CharField(source='get_method_display', read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SupplierPayment
        fields = ['id', 'supplier', 'amount', 'date', 'method', 'method_display',
                  'note', 'created_by_name', 'created_at']
        read_only_fields = ['id', 'method_display', 'created_by_name', 'created_at']

    def get_created_by_name(self, obj):
        u = obj.created_by
        return (u.get_full_name() or u.username) if u else None

    def validate_amount(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError('Payment amount must be greater than zero.')
        return value

    def validate_supplier(self, value):
        # Tenant guard: the supplier must belong to the caller's store.
        request = self.context.get('request')
        if request and value.store_id != request.user.store_id:
            raise serializers.ValidationError('Supplier not found in this store.')
        return value


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
    net_refund = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True)
    customer_name = serializers.CharField(source='customer.name', read_only=True)

    class Meta:
        model = RefundInvoice
        fields = [
            'id', 'branch', 'original_invoice', 'customer', 'customer_name',
            'refund_number', 'date', 'total_refunded', 'reason', 'items',
            'restocking_fee', 'refund_method', 'net_refund',
        ]
        # restocking_fee is computed server-side from the store's policy %, not
        # client-supplied, so a caller can't zero it out.
        read_only_fields = ['id', 'refund_number', 'date', 'total_refunded',
                            'restocking_fee', 'net_refund']

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        original = validated_data.get('original_invoice')
        store = validated_data.get('store')
        with transaction.atomic():
            if original is not None:
                self._validate_against_original(original, items_data)
                self._validate_return_window(store, original)
            refund = RefundInvoice.objects.create(**validated_data)
            for item_data in items_data:
                RefundItem.objects.create(refund=refund, **item_data)
            # RefundItem.save() recomputed total_refunded on a separate instance;
            # reload so we see the gross total here.
            refund.refresh_from_db()
            # Restocking fee = store policy % of the gross refund.
            pct = self._store_restock_percent(store)
            if pct:
                refund.restocking_fee = (
                    (refund.total_refunded * pct / Decimal('100'))
                    .quantize(Decimal('0.01'))
                )
                refund.save(update_fields=['restocking_fee'])
            # Store-credit payout accrues to the customer's wallet (net of fee).
            if (refund.refund_method == RefundInvoice.RefundMethod.STORE_CREDIT
                    and refund.customer_id):
                from users.models import Customer
                cust = (Customer.all_objects.select_for_update()
                        .get(pk=refund.customer_id))
                cust.store_credit = (cust.store_credit or Decimal('0')) + refund.net_refund
                cust.save(update_fields=['store_credit'])
        return refund

    @staticmethod
    def _store_restock_percent(store):
        from core.models import StoreSettings
        pct = (StoreSettings.objects.filter(store=store)
               .values_list('restocking_fee_percent', flat=True).first())
        return pct or Decimal('0')

    @staticmethod
    def _validate_return_window(store, original):
        """Reject a return if the original invoice is older than the store's
        return window. 0 = no limit."""
        from core.models import StoreSettings
        from django.utils import timezone
        days = (StoreSettings.objects.filter(store=store)
                .values_list('return_window_days', flat=True).first()) or 0
        if days and original and original.date:
            elapsed = (timezone.now() - original.date).days
            if elapsed > days:
                raise serializers.ValidationError(
                    f"Return window expired: the original invoice is {elapsed} "
                    f"days old, but this store only accepts returns within "
                    f"{days} days."
                )

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
