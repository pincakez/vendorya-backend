from decimal import Decimal
from rest_framework import serializers
from django.db import transaction
from django.db.models import Sum
from inventory.models import ProductUnit
from .models import (
    SalesInvoice, SalesInvoiceItem, Payment, PaymentMethod, CustomerReceipt,
    PurchaseInvoice, PurchaseItem, SupplierPayment, PurchaseReturn, PurchaseReturnItem, Cheque,
    Expense, ExpenseCategory,
    WorkShift,
    RefundInvoice, RefundItem,
    customer_outstanding, invoice_outstanding,
)


def enforce_credit_policy(invoice, paid_amount=None):
    """Apply the store's credit policy (ALLOW / WARN / BLOCK) to a sale whose
    unpaid balance would push the customer past their credit limit.

    Outstanding is computed LIVE from the customer's posted invoices
    (`customer_outstanding`), since `Customer.balance` is not maintained.
    Call this at the moment credit is actually extended — i.e. when posting
    (checkout), not when a draft cart is created. Raises ValidationError on BLOCK.
    """
    # `paid_amount` = what WILL be paid when the till pays in the same step as checkout (s156).
    paid = invoice.paid_amount if paid_amount is None else paid_amount
    unpaid = invoice.grand_total - paid
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
        from notifications.dispatcher import send_notification, msg
        from notifications.models import Notification
        send_notification(
            store=store,
            title=f"Credit limit exceeded: {customer.name}",
            body=(f"Invoice #{invoice.invoice_number or '(draft)'} — unpaid {unpaid}. "
                  f"New balance {new_balance} exceeds limit {effective_limit}."),
            priority=Notification.Priority.WARNING,
            link="/people/customers",
            i18n=msg('credit_limit.title', 'credit_limit.body',   # s167
                     customer=customer.name, number=invoice.invoice_number or '—',
                     unpaid=unpaid, balance=new_balance, limit=effective_limit),
        )


def _request_store(serializer):
    request = serializer.context.get('request')
    return getattr(getattr(request, 'user', None), 'store', None)


def _treasury_on(serializer):
    from treasury import ledger
    return ledger.is_on(_request_store(serializer))


def _box_field():
    """A money box picked by id. Unscoped queryset on purpose: the tenant check is done in validate() with a clean
    `code` (another shop's box answers "not found", exactly like a box that doesn't exist)."""
    from treasury.models import MoneyBox
    return serializers.PrimaryKeyRelatedField(queryset=MoneyBox.all_objects.all(), required=False, allow_null=True)


class PaymentMethodSerializer(serializers.ModelSerializer):
    # TREASURY.md § 4.1: the box this method's money lands in (Treasury on only; never cash / Ajel / store credit).
    lands_in = _box_field()
    lands_in_name = serializers.CharField(source='lands_in.name', read_only=True, default=None)

    class Meta:
        model = PaymentMethod
        fields = ['id', 'name', 'is_cash', 'is_agel', 'is_store_credit', 'lands_in', 'lands_in_name']
        read_only_fields = ['id', 'is_store_credit']

    def validate(self, attrs):
        box = attrs.get('lands_in')
        if box is None:
            return attrs
        from .treasury_flows import check_box, refuse
        from treasury.models import MoneyBox
        if not _treasury_on(self):
            refuse('treasury_off', "Turn the Treasury on to choose where a payment method's money lands.")
        check_box(box, _request_store(self), 'lands_in')
        if box.kind == MoneyBox.Kind.DRAWER:
            refuse('drawer_not_allowed', 'Cash lands in the till drawer by itself — choose another box.')
        inst = self.instance
        is_cash = attrs.get('is_cash', getattr(inst, 'is_cash', False))
        is_agel = attrs.get('is_agel', getattr(inst, 'is_agel', False))
        if is_cash or is_agel or getattr(inst, 'is_store_credit', False):
            refuse('lands_in_not_allowed', "Cash, Ajel and store credit don't land in a chosen box.")
        return attrs


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


class SerialListField(serializers.ListField):
    """A list of serial numbers (New 7) — normalised (no spaces, upper-case), blanks dropped, a serial
    typed twice refused. Whether the count is right is checked where the stock moves."""
    child = serializers.CharField(max_length=120, allow_blank=True)

    def to_internal_value(self, data):
        from inventory.serials import clean_list
        if isinstance(data, str):
            return clean_list(data)
        return clean_list(super().to_internal_value(data))


def _own_staff(serializer, user):
    """A salesperson must be a staff login of the SAME shop (never another shop's person, never sudo)."""
    if user is None:
        return None
    request = serializer.context.get('request')
    store = getattr(getattr(request, 'user', None), 'store', None)
    if store is None or user.store_id != store.id or getattr(user, 'is_superadmin', False):
        raise serializers.ValidationError('Choose a person of this shop.')
    return user


class SalesInvoiceItemSerializer(serializers.ModelSerializer):
    # New 7: the serials the till picked for a serial-tracked line (checked + applied at checkout).
    serial_numbers = SerialListField(required=False)

    class Meta:
        model = SalesInvoiceItem
        fields = ['id', 'variant', 'unit', 'unit_factor', 'quantity', 'unit_price',
                  'discount_amount', 'tax_amount', 'total',
                  'serial_numbers', 'condition', 'warranty_months', 'warranty_ends_on',
                  'salesperson', 'salesperson_name']
        # condition + warranty are frozen from the product when the sale is posted (New 8).
        read_only_fields = ['id', 'unit_factor', 'tax_amount', 'total',
                            'condition', 'warranty_months', 'warranty_ends_on', 'salesperson_name']

    def validate_salesperson(self, value):
        return _own_staff(self, value)


class SalesInvoiceSerializer(serializers.ModelSerializer):
    items = SalesInvoiceItemSerializer(many=True, required=False)
    # s168: the invoice list + Returns window showed the customer's id (or nothing) — send the name.
    customer_name = serializers.CharField(source='customer.name', read_only=True, default='')

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'branch', 'customer', 'customer_name', 'invoice_number', 'status', 'date',
            'subtotal', 'tax_total', 'discount', 'grand_total', 'paid_amount', 'change_given',
            'items', 'created_at', 'salesperson', 'salesperson_name',
        ]
        read_only_fields = [
            'id', 'invoice_number', 'subtotal', 'tax_total',
            'grand_total', 'paid_amount', 'change_given', 'created_at', 'salesperson_name',
        ]

    def validate_salesperson(self, value):
        return _own_staff(self, value)

    def validate_status(self, value):
        # 2026-10-14: a sale is BORN a draft. Creating it already POSTED skipped every checkout rule (open shift,
        # credit selling off, stock, quota) — even for a cashier. Complete it with checkout, or a manager's Post.
        if self.instance is None and value and value != SalesInvoice.Status.DRAFT:
            raise serializers.ValidationError('A new sale starts as a draft — complete it with checkout.')
        return value

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        validated_data['status'] = SalesInvoice.Status.DRAFT
        with transaction.atomic():
            invoice = SalesInvoice.objects.create(**validated_data)
            for item_data in items_data:
                SalesInvoiceItem.objects.create(invoice=invoice, **_stamp_unit_factor(item_data))
            self._recalculate(invoice)  # authoritative: sets every line's tax + invoice totals
        return invoice

    @staticmethod
    def _line_tax(invoice, variant):
        """(rate %, tax name) that applies to a line: the product's own tax, else the
        store default, else 0 — and always 0 when tax is disabled for the store."""
        settings = getattr(invoice.store, 'settings', None)
        if settings is not None and not getattr(settings, 'tax_enabled', True):
            return Decimal('0'), ''
        tax = getattr(getattr(variant, 'product', None), 'tax', None)
        if tax is None and settings is not None:
            tax = settings.default_tax
        return (tax.rate, tax.name) if tax is not None else (Decimal('0'), '')

    @staticmethod
    def _line_tax_rate(invoice, variant):
        return SalesInvoiceSerializer._line_tax(invoice, variant)[0]

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        with transaction.atomic():
            if 'salesperson' in validated_data and validated_data['salesperson'] != instance.salesperson:
                instance.salesperson_name = ''        # re-frozen from the new person in save()
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
        priced = SalesInvoiceSerializer.price_lines(
            invoice, [(it.variant, it.quantity, it.unit_price, it.discount_amount) for it in items])
        for item, line in zip(items, priced['lines']):
            rate, tax_name, taxable, tax = line['tax_rate'], line['tax_name'], line['taxable_amount'], line['tax_amount']
            # s165 (§AUDIT A12): freeze the rate/name/base on the line for the tax report.
            if (Decimal(str(item.tax_amount or '0')) != tax or item.tax_rate != rate
                    or item.tax_name != tax_name or item.taxable_amount != taxable):
                item.tax_amount, item.tax_rate, item.tax_name, item.taxable_amount = tax, rate, tax_name, taxable
                item.save(update_fields=['tax_amount', 'total', 'tax_rate', 'tax_name', 'taxable_amount'])  # save() recomputes total

        invoice.subtotal = priced['subtotal']
        invoice.tax_total = priced['tax_total']
        invoice.grand_total = priced['grand_total']
        invoice.save(update_fields=['subtotal', 'tax_total', 'grand_total'])

    @staticmethod
    def price_lines(invoice, lines):
        """The sale pricing rule (_recalculate's docstring), WITHOUT saving anything — so a quote (e.g. a service
        job before Done, Upgrade 13) shows exactly what the invoice will say. `invoice` only supplies the shop
        (`.store`) and the whole-invoice `.discount`; `lines` = [(variant, quantity, unit_price, discount_amount)].
        Returns {'lines': [{tax_rate, tax_name, taxable_amount, tax_amount, total}], subtotal, tax_total, grand_total}."""
        line_nets = [
            (Decimal(str(qty)) * Decimal(str(price))) - Decimal(str(disc or '0'))
            for _v, qty, price, disc in lines
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

        out, tax_total = [], Decimal('0')
        for (variant, _q, _p, _d), line_net in zip(lines, line_nets):
            share = (invoice_discount * line_net / discounted_subtotal) if discounted_subtotal > 0 else Decimal('0')
            taxable = line_net - share
            rate, tax_name = SalesInvoiceSerializer._line_tax(invoice, variant)
            tax = (taxable * rate / Decimal('100')).quantize(Decimal('0.01'))
            out.append({'tax_rate': rate, 'tax_name': tax_name, 'taxable_amount': taxable.quantize(Decimal('0.01')),
                        'tax_amount': tax, 'total': line_net + tax})   # = SalesInvoiceItem.save()'s total
            tax_total += tax

        return {'lines': out, 'subtotal': discounted_subtotal, 'tax_total': tax_total,
                'grand_total': discounted_subtotal - invoice_discount + tax_total}


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ['id', 'invoice', 'method', 'amount', 'created_by', 'created_at']
        read_only_fields = ['id', 'created_by', 'created_at']

    def validate(self, attrs):
        """A payment is money actually received against a POSTED sale (s156, §AUDIT A2): never an Ajel
        method (credit is the UNPAID part, not a payment), never zero/negative, never more than is owed."""
        from decimal import Decimal
        from django.db.models import Sum
        invoice, method, amount = attrs['invoice'], attrs['method'], attrs['amount']
        if method.store_id != invoice.store_id:
            raise serializers.ValidationError({'method': 'Unknown payment method.'})
        if method.is_agel:
            raise serializers.ValidationError({'method': 'Ajel (credit) is not a payment — leave the sale unpaid instead.'})
        if method.is_store_credit:   # s167: store credit is spent only at checkout, where the wallet goes down
            raise serializers.ValidationError({'method': 'Unknown payment method.'})
        if amount <= 0:
            raise serializers.ValidationError({'amount': 'Amount must be more than zero.'})
        if invoice.status != SalesInvoice.Status.POSTED:
            raise serializers.ValidationError({'invoice': 'Payments can only be taken on a completed sale.'})
        owed = invoice_outstanding(invoice)
        if amount > owed + Decimal('0.005'):
            raise serializers.ValidationError({'amount': f'More than is owed on this sale ({owed}).'})
        return attrs


class CustomerReceiptSerializer(serializers.ModelSerializer):
    """A lump-sum debt payment from a customer (s167). Write: customer, amount, method, date?, note?,
    allow_advance? — the spreading over sales is done by finance.receipts.receive_customer_payment."""
    allow_advance = serializers.BooleanField(write_only=True, required=False, default=False)
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    method_name = serializers.CharField(source='method.name', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    allocations = serializers.SerializerMethodField()

    class Meta:
        model = CustomerReceipt
        fields = ['id', 'customer', 'customer_name', 'amount', 'method', 'method_name', 'date', 'note',
                  'unallocated', 'advance', 'allocations', 'allow_advance', 'created_by_name', 'created_at']
        read_only_fields = ['id', 'unallocated', 'advance', 'created_at']

    def get_created_by_name(self, obj):
        u = obj.created_by
        return (u.get_full_name() or u.username) if u else None

    def get_allocations(self, obj):
        return [{'invoice': str(p.invoice_id), 'invoice_number': p.invoice.invoice_number, 'amount': str(p.amount)}
                for p in obj.payments.filter(is_deleted=False).select_related('invoice').order_by('invoice__date')]

    def validate_amount(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError('Amount must be more than zero.')
        return value

    def validate(self, attrs):
        request = self.context.get('request')
        store_id = request.user.store_id if request else None
        customer, method = attrs['customer'], attrs['method']
        if customer.store_id != store_id or customer.is_deleted:      # tenant guard
            raise serializers.ValidationError({'customer': 'Customer not found in this store.'})
        if method.store_id != store_id or method.is_deleted:
            raise serializers.ValidationError({'method': 'Unknown payment method.'})
        if method.is_agel:
            raise serializers.ValidationError({'method': 'Ajel (credit) is not a payment — choose how the money came in.'})
        if method.is_store_credit:   # s167: store credit is spent only at checkout, where the wallet goes down
            raise serializers.ValidationError({'method': 'Unknown payment method.'})
        return attrs


# --- PURCHASE ---

class PurchaseItemSerializer(serializers.ModelSerializer):
    track_expiry = serializers.BooleanField(source='variant.product.track_expiry', read_only=True)
    track_serials = serializers.BooleanField(source='variant.product.track_serials', read_only=True)

    class Meta:
        model = PurchaseItem
        fields = ['id', 'variant', 'unit', 'unit_factor', 'quantity', 'unit_cost', 'total_cost',
                  'expiry_date', 'batch_number', 'track_expiry', 'serial_numbers', 'track_serials']
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
                'track_serials': v.product.track_serials,
                'serial_numbers': list(it.serial_numbers or []),
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
                         track_expiry=False, track_serials=False):
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
        extra = {}
        if track_expiry:
            extra['track_expiry'] = True
        if track_serials:   # New 7: a brand-new laptop / phone model tracked by serial from its first receipt
            extra['track_serials'] = True
        product = create_product_with_variant(
            invoice.store, name=name, supplier=invoice.supplier, category=category,
            base_price=base, cost_price=base, sell_price=retail,
            attributes=attributes or [],
            extra_product_fields=extra or None,
            auto_mb_units=True,   # §MU-MB: seed Strip/Pack tiers from the drug reference
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
            # New 7: one serial per unit for a serial-tracked product (a list, or a pasted text column).
            # A draft may be incomplete; receive checks the count exactly.
            from inventory.serials import clean_list
            serials = clean_list(line.get('serial_numbers'), name)
            track_serials = bool(line.get('track_serials'))

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
                if variant.product.track_serials:
                    from inventory.serials import whole_units
                    whole_units(variant.product.name, qty, factor)   # base unit, whole units only
                else:
                    serials = []
                PurchaseItem.objects.create(invoice=invoice, variant=variant,
                                            unit=unit, unit_factor=factor,
                                            quantity=qty, unit_cost=base,
                                            expiry_date=expiry, batch_number=batch,
                                            serial_numbers=serials)
            elif invoice.supplier_id:
                # new product, supplier present → materialize now
                cat_id  = line.get('subcategory') or line.get('category')
                variant = self._materialize_new(
                    invoice, name, cat_id, line.get('attributes'),
                    base, self._dec(retail), track_expiry=track, track_serials=track_serials,
                )
                if track_serials:
                    from inventory.serials import whole_units
                    whole_units(name, qty)
                PurchaseItem.objects.create(invoice=invoice, variant=variant,
                                            quantity=qty, unit_cost=base,
                                            expiry_date=expiry, batch_number=batch,
                                            serial_numbers=serials if track_serials else [])
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
                    'track_serials': track_serials,
                    'serial_numbers': serials if track_serials else [],
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
                track_serials=bool(line.get('track_serials')),
            )
            PurchaseItem.objects.create(
                invoice=invoice, variant=variant,
                quantity=self._dec(line.get('quantity'), '1'), unit_cost=base,
                expiry_date=line.get('expiry_date') or None,
                batch_number=(line.get('batch_number') or '').strip(),
                serial_numbers=list(line.get('serial_numbers') or []) if line.get('track_serials') else [],
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
    """A payment TO a supplier. Write: supplier, amount, payment_method (one of the shop's own methods —
    not Ajel, not store credit), date?, note?.

    PROLINE-PARITY Upgrade 6: `payment_method` replaced the fixed CASH/BANK/CARD/OTHER `method` code. An old
    client that still sends only `method` keeps working — the code is mapped to the shop's matching method
    (supplier_payment_methods.pick_method_for_legacy, the same rule the data migration used); a code with no
    match is refused, so every NEW payment has a real method. Read: `payment_method_name` = the method's name,
    or the old code's label for an old payment the migration could not map."""
    method_display = serializers.CharField(source='get_method_display', read_only=True)
    payment_method_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SupplierPayment
        fields = ['id', 'supplier', 'amount', 'date', 'payment_method', 'payment_method_name',
                  'method', 'method_display', 'paid_from', 'paid_from_name', 'note', 'created_by_name', 'created_at']
        read_only_fields = ['id', 'payment_method_name', 'method_display', 'created_by_name', 'created_at']
        extra_kwargs = {'method': {'required': False}}

    # TREASURY.md: the box the money left from (Treasury on: required — defaults to the method's box).
    paid_from = _box_field()
    paid_from_name = serializers.CharField(source='paid_from.name', read_only=True, default=None)

    def get_fields(self):
        # Tenant guard (defence in depth): an id from the body resolves ONLY among the caller's shop's
        # methods — another shop's id is "does not exist", never a match. validate() refuses the rest.
        fields = super().get_fields()
        request = self.context.get('request')
        store_id = getattr(getattr(request, 'user', None), 'store_id', None)
        fields['payment_method'].queryset = PaymentMethod.all_objects.filter(store_id=store_id)
        return fields

    def get_created_by_name(self, obj):
        u = obj.created_by
        return (u.get_full_name() or u.username) if u else None

    def get_payment_method_name(self, obj):
        return obj.payment_method.name if obj.payment_method_id else obj.get_method_display()

    def validate(self, attrs):
        from .supplier_payment_methods import pick_method_for_legacy, legacy_code_for
        request = self.context.get('request')
        store_id = request.user.store_id if request else None
        method = attrs.get('payment_method')
        if method is None and 'method' in attrs:
            # Old client: map the fixed code to the shop's own method (or refuse — no new unmapped rows).
            shop_methods = PaymentMethod.objects.filter(store_id=store_id).order_by('created_at', 'id')
            method = pick_method_for_legacy(shop_methods, attrs['method'])
            if method is None:
                raise serializers.ValidationError(
                    {'payment_method': 'Choose one of the shop\'s payment methods.'})
            attrs['payment_method'] = method
        if method is None:
            if self.instance is None or 'payment_method' in attrs:   # new payment, or clearing it
                raise serializers.ValidationError({'payment_method': 'Choose how you paid the supplier.'})
            return _validate_paid_from(self, attrs)
        if method.store_id != store_id or method.is_deleted:
            raise serializers.ValidationError({'payment_method': 'This payment method was not found in your shop.'})
        if method.is_agel:
            raise serializers.ValidationError(
                {'payment_method': 'Ajel (credit) is not a payment — choose how the money was actually paid.'})
        if method.is_store_credit:
            raise serializers.ValidationError(
                {'payment_method': 'Store credit belongs to customers — it cannot pay a supplier.'})
        attrs['method'] = legacy_code_for(method)   # keep the old column truthful for anything still reading it
        return _validate_paid_from(self, attrs, default=lambda: _supplier_default_box(self, method))

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


def _supplier_default_box(serializer, method):
    from .treasury_flows import default_box_for_supplier
    request = serializer.context.get('request')
    return default_box_for_supplier(method, getattr(request, 'user', None), _request_store(serializer))


def _validate_paid_from(serializer, attrs, default=None):
    """`paid_from` for an expense / supplier payment (TREASURY.md § 4). Off: ignored (today's behaviour). On: a new
    row needs one (from the body, else `default()`), it must be one of the shop's open boxes, it can't be cleared,
    and a row made before the Treasury can't be put in a box afterwards."""
    if not _treasury_on(serializer):
        attrs.pop('paid_from', None)
        return attrs
    from .treasury_flows import check_box, refuse
    inst = serializer.instance
    if inst is None:
        if attrs.get('paid_from') is None and default is not None:
            attrs['paid_from'] = default()
        if attrs.get('paid_from') is None:
            refuse('paid_from_required', 'Choose the money box this was paid from.')
    elif 'paid_from' in attrs:
        if attrs['paid_from'] is None:
            refuse('paid_from_required', 'Choose the money box this was paid from.')
        if inst.paid_from_id is None:
            refuse('before_treasury', 'This was recorded before the Treasury was on — it stays out of the boxes.')
    check_box(attrs.get('paid_from'), _request_store(serializer), 'paid_from')
    return attrs


class ExpenseSerializer(serializers.ModelSerializer):
    # TREASURY.md Upgrade 7: the box the money left from (Treasury on: required).
    paid_from = _box_field()
    paid_from_name = serializers.CharField(source='paid_from.name', read_only=True, default=None)

    class Meta:
        model = Expense
        # owner_only (TREASURY.md § 12.4): the owner carries it alone — partners' profit share ignores it. Flipping it
        # never touches a box line (only amount / box do).
        fields = ['id', 'branch', 'category', 'amount', 'description', 'date', 'paid_from', 'paid_from_name',
                  'owner_only', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate(self, attrs):
        # Who carries an expense moves money between the owner and the partners at the next settlement → only the
        # owner (the person, TREASURY.md § 12.0) may set or clear it. Re-sending the current value is fine.
        if 'owner_only' in attrs:
            current = self.instance.owner_only if self.instance is not None else False
            if attrs['owner_only'] != current:
                from treasury import ledger
                request = self.context.get('request')
                if request is None or ledger.owner_for_user(request.user) is None:
                    raise serializers.ValidationError(
                        {'owner_only': 'Only the owner decides who carries an expense.', 'code': 'not_owner'})
        return _validate_paid_from(self, attrs)


# --- SHIFTS ---

class WorkShiftSerializer(serializers.ModelSerializer):
    # TREASURY.md § 4.2 (Treasury on): the shift is a session on a DRAWER box. `drawer` is chosen when opening
    # (default = the branch's default drawer); `book_balance` = what the drawer's book said when the shift opened
    # (carried over); `starting_cash` = what was counted; a difference needs `count_reason` (write-only).
    drawer = _box_field()
    drawer_name = serializers.CharField(source='drawer.name', read_only=True, default=None)
    book_balance = serializers.SerializerMethodField()
    count_reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = WorkShift
        fields = [
            'id', 'branch', 'user', 'start_time', 'end_time', 'status',
            'starting_cash', 'closing_cash', 'expected_cash', 'difference',
            'ledger', 'drawer', 'drawer_name', 'book_balance', 'count_reason',
        ]
        read_only_fields = [
            'id', 'user', 'start_time', 'end_time', 'status',
            'expected_cash', 'difference', 'closing_cash', 'ledger',
        ]

    def get_book_balance(self, obj):
        from .treasury_flows import carried_over
        bal = carried_over(obj)
        return None if bal is None else str(bal)

    def update(self, instance, validated_data):
        validated_data.pop('drawer', None)          # the drawer is fixed once the shift is open
        validated_data.pop('count_reason', None)
        return super().update(instance, validated_data)


# --- REFUNDS ---

class RefundItemSerializer(serializers.ModelSerializer):
    # s168: the unit the customer brings back (e.g. 1 Pack). Write-only — the line freezes its
    # unit_factor from it (same guard as a sale line: a unit of another product falls back to the
    # base unit), so a returned Pack restocks 30 tablets, not 1. Omitted = the base unit.
    unit = serializers.PrimaryKeyRelatedField(
        queryset=ProductUnit.objects.filter(is_deleted=False),
        required=False, allow_null=True, write_only=True)
    # New 7: the serials coming back on a serial-tracked line (each must have been sold on that sale).
    serial_numbers = SerialListField(required=False)

    class Meta:
        model = RefundItem
        fields = ['id', 'variant', 'unit', 'unit_factor', 'quantity', 'refund_amount', 'restock_inventory',
                  'serial_numbers']
        read_only_fields = ['id', 'unit_factor']

    def validate(self, attrs):
        return _stamp_unit_factor(attrs)

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
            'restocking_fee', 'refund_method', 'net_refund', 'drawer_cash', 'paid_back', 'store_credit_back',
        ]
        # restocking_fee is computed server-side from the store's policy %, not
        # client-supplied, so a caller can't zero it out.
        read_only_fields = ['id', 'refund_number', 'date', 'total_refunded',
                            'restocking_fee', 'net_refund', 'drawer_cash', 'paid_back', 'store_credit_back']

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        original = validated_data.get('original_invoice')
        store = validated_data.get('store')
        with transaction.atomic():
            if original is not None:
                self._validate_against_original(original, items_data)
                self._validate_return_window(store, original)
            from inventory import serials as serial_engine
            # New 7: take the per-shop serial lock BEFORE any StockLevel row lock (RefundItem.save) — receive /
            # register take it in that order too, so a return and a receive at the same moment can't deadlock.
            if any(serial_engine.is_tracked(i['variant']) for i in items_data):
                serial_engine.lock_shop_serials(store)
            refund = RefundInvoice.objects.create(**validated_data)
            for item_data in items_data:
                item_data.pop('unit', None)   # only its factor is kept (frozen by RefundItemSerializer)
                ri = RefundItem.objects.create(refund=refund, **item_data)
                # New 7: a serial-tracked line's units come back (checked against the sale) — raises → rollback.
                serial_engine.return_units(ri, original=original, user=validated_data.get('created_by'))
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
            self._record_paid_back(refund, original)
            self._record_store_credit_back(refund, original)
            if refund.refund_method == RefundInvoice.RefundMethod.CASH:
                self._record_drawer_cash(refund, original)
            # TREASURY.md § 4: the money handed back leaves its boxes (a no-op with the Treasury off).
            from .treasury_flows import post_refund
            post_refund(refund, original, validated_data.get('created_by'))
            # The wallet part of the payout accrues to the customer's store credit — only what is really paid
            # back (s165): all of it on a Store Credit refund; on a Cash / Original refund, the share of the
            # sale that was paid WITH store credit (s167).
            if refund.customer_id and refund.store_credit_back > 0:
                from users.models import Customer
                cust = (Customer.all_objects.select_for_update()
                        .get(pk=refund.customer_id))
                cust.store_credit = (cust.store_credit or Decimal('0')) + refund.store_credit_back
                cust.save(update_fields=['store_credit'])
        return refund

    @staticmethod
    def _record_store_credit_back(refund, original):
        """s167 (split payment): how much of paid_back goes into the customer's store-credit wallet.
        Store Credit refund → all of it. Cash / Original refund → first the part of the sale that was paid with
        store credit (less what earlier returns already put back in the wallet), so store credit is never
        turned into cash; the rest is cash (then card) as before."""
        if refund.refund_method == RefundInvoice.RefundMethod.STORE_CREDIT:
            back = refund.paid_back
        elif original is not None:
            spent = (Payment.objects.filter(invoice=original, method__is_store_credit=True)
                     .aggregate(s=Sum('amount'))['s'] or Decimal('0'))
            earlier = (RefundInvoice.objects.filter(original_invoice=original, is_deleted=False)
                       .exclude(pk=refund.pk).aggregate(s=Sum('store_credit_back'))['s'] or Decimal('0'))
            back = min(refund.paid_back, spent - earlier)
        else:
            back = Decimal('0')
        refund.store_credit_back = max(back, Decimal('0'))
        refund.save(update_fields=['store_credit_back'])

    @staticmethod
    def _record_paid_back(refund, original):
        """s165 (§WALK-S163 B1): how much money goes back to the customer (cash, card or wallet). With an
        original sale: never more than was paid on it, less what earlier returns already paid back — the
        unpaid (Ajel) part of the refund only cancels debt. Without one: the whole net refund."""
        back = refund.net_refund
        if original is not None:
            SalesInvoice.all_objects.select_for_update().filter(pk=original.pk).first()   # serialise returns on one sale
            original.refresh_from_db(fields=['paid_amount'])
            earlier = (RefundInvoice.objects.filter(original_invoice=original, is_deleted=False)
                       .exclude(pk=refund.pk).aggregate(s=Sum('paid_back'))['s'] or Decimal('0'))
            back = min(back, (original.paid_amount or Decimal('0')) - earlier)
        refund.paid_back = max(back, Decimal('0'))
        refund.save(update_fields=['paid_back'])

    @staticmethod
    def _record_drawer_cash(refund, original):
        """s157 (§AUDIT A9): fix how much of a "Cash / Original" payout left a till, and which one.
        With an original sale, only the cash taken on it (less cash already given back) goes back in cash;
        the rest returns to the card / cancels Ajel debt. The drawer = the refunder's own open shift in
        this branch, else the branch's most recently opened one; none open → paid from outside a till."""
        cash = refund.paid_back - refund.store_credit_back   # s167: the wallet share never leaves the till as cash
        if original is not None:
            taken = (Payment.objects.filter(invoice=original, method__is_cash=True)
                     .aggregate(s=Sum('amount'))['s'] or Decimal('0'))
            given = (RefundInvoice.objects.filter(original_invoice=original, is_deleted=False)
                     .exclude(pk=refund.pk).aggregate(s=Sum('drawer_cash'))['s'] or Decimal('0'))
            cash = min(cash, taken - given)
        refund.drawer_cash = max(cash, Decimal('0'))
        from treasury import ledger
        if ledger.is_on(refund.store):
            # Treasury on: the cash leaves the refunder's OWN drawer (any branch — one open shift per person, the same
            # rule as sales + receipts), never a colleague's; no shift → the refund branch's default drawer (post_refund).
            refund.shift = WorkShift.objects.filter(store=refund.store, user=refund.created_by,
                                                    status=WorkShift.Status.OPEN).first()
        else:
            open_shifts = WorkShift.objects.filter(store=refund.store, branch=refund.branch,
                                                   status=WorkShift.Status.OPEN)
            refund.shift = (open_shifts.filter(user=refund.created_by).first()
                            or open_shifts.order_by('-start_time').first())
        refund.save(update_fields=['drawer_cash', 'shift'])

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
        s165: the MONEY is capped too — a line may return at most what those units were sold for
        (line total incl. tax, after its discount, per unit), never more than is left unrefunded on
        that product, and the whole refund never more than the sale's grand total left unrefunded.
        Only enforced when the refund references an original invoice."""
        TOL = Decimal('0.01')
        # s168: every quantity is counted in BASE units (qty × the line's frozen unit_factor), so a
        # pharmacy that sold 1 Pack (30 tablets) can take back 1 Pack, or 1 Strip + 20 tablets — and
        # can never take back 30 Packs because 30 "units" were sold.
        def base(qty, factor):
            return Decimal(str(qty)) * Decimal(str(factor or 1))
        from .models import returned_so_far   # the one sold / already-returned count (shared with the service desk)
        counts = returned_so_far(original)
        sold = {v: r['sold'] for v, r in counts.items()}
        sold_value = {v: r['sold_value'] for v, r in counts.items()}
        already = {v: r['returned'] for v, r in counts.items()}
        already_value = {v: r['returned_value'] for v, r in counts.items()}
        requested, requested_value = {}, {}
        for item in items_data:
            v = item['variant']
            requested[v.id] = requested.get(v.id, Decimal('0')) + base(item['quantity'], item.get('unit_factor'))
            requested_value[v.id] = requested_value.get(v.id, Decimal('0')) + (item.get('refund_amount') or 0)

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
                    f"refundable (sold {sold_q}, already refunded {already.get(vid, Decimal('0'))}; "
                    f"counted in the product's base unit)."
                )
                continue
            per_unit = sold_value.get(vid, Decimal('0')) / sold_q
            max_money = min(per_unit * req, sold_value.get(vid, Decimal('0')) - already_value.get(vid, Decimal('0')))
            if requested_value.get(vid, Decimal('0')) > max_money + Decimal('0.005'):
                errors.append(
                    f"Cannot refund {requested_value[vid]} for {req} of variant {vid} — at most "
                    f"{max_money.quantize(TOL)} (what they were sold for, less earlier refunds)."
                )
        if not errors:
            earlier = (RefundInvoice.objects.filter(original_invoice=original, is_deleted=False)
                       .aggregate(s=Sum('total_refunded'))['s'] or Decimal('0'))
            left = (original.grand_total or Decimal('0')) - earlier
            total_req = sum(requested_value.values(), Decimal('0'))
            if total_req > left + Decimal('0.005'):
                errors.append(f"Cannot refund {total_req} — only {left} of this sale is left to refund.")
        if errors:
            raise serializers.ValidationError({'items': errors})


# --- RETURN TO SUPPLIER (s171, finance/purchase_returns.py) ---

class PurchaseReturnItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='variant.product.name', read_only=True)
    sku = serializers.CharField(source='variant.sku', read_only=True)
    unit_name = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseReturnItem
        fields = ['id', 'variant', 'product_name', 'sku', 'purchase_item', 'unit', 'unit_name', 'unit_factor',
                  'quantity', 'unit_cost', 'total_cost', 'serial_numbers']
        read_only_fields = fields

    def get_unit_name(self, obj):
        return obj.unit.name if obj.unit_id else ''


class PurchaseReturnSerializer(serializers.ModelSerializer):
    """Read shape. Writing goes through PurchaseReturnViewSet.create → purchase_returns.create_return (one door)."""
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    purchase_number = serializers.SerializerMethodField()
    refund_method_name = serializers.SerializerMethodField()
    refund_box_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    credit_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    items = PurchaseReturnItemSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseReturn
        fields = ['id', 'return_number', 'date', 'status', 'supplier', 'supplier_name', 'branch', 'branch_name',
                  'purchase', 'purchase_number', 'total_amount', 'refund_amount', 'credit_amount', 'refund_method',
                  'refund_method_name', 'refund_box', 'refund_box_name', 'notes', 'void_reason', 'created_by_name',
                  'created_at', 'items']
        read_only_fields = fields

    def get_purchase_number(self, obj):
        p = obj.purchase
        return (p.purchase_number or p.vendor_reference or '') if p else ''

    def get_refund_method_name(self, obj):
        return obj.refund_method.name if obj.refund_method_id else ''

    def get_refund_box_name(self, obj):
        return obj.refund_box.name if obj.refund_box_id else ''

    def get_created_by_name(self, obj):
        u = obj.created_by
        return (u.get_full_name() or u.username) if u else ''


# --- CHEQUES (s171, finance/cheques.py) ---

class ChequeSerializer(serializers.ModelSerializer):
    """Read shape. Writes go through ChequeViewSet → finance/cheques.py (one door)."""
    party_name = serializers.CharField(read_only=True)
    method_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    days_to_due = serializers.SerializerMethodField()

    class Meta:
        model = Cheque
        fields = ['id', 'direction', 'customer', 'supplier', 'party_name', 'number', 'bank_name', 'amount',
                  'issue_date', 'due_date', 'days_to_due', 'status', 'method', 'method_name', 'cleared_on',
                  'customer_receipt', 'supplier_payment', 'status_reason', 'note', 'created_by_name', 'created_at']
        read_only_fields = fields

    def get_method_name(self, obj):
        return obj.method.name if obj.method_id else ''

    def get_created_by_name(self, obj):
        u = obj.created_by
        return (u.get_full_name() or u.username) if u else ''

    def get_days_to_due(self, obj):
        from django.utils import timezone as tz
        return (obj.due_date - tz.localdate()).days
