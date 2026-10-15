from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from core.field_visibility import hides_cost, request_user
from .models import Service, ServicePart, ServiceNote


class _ShopVariantField(serializers.PrimaryKeyRelatedField):
    """Only the caller's shop's live, sellable variants — never another shop's id, a Memory Base entry, a
    deleted product or the system "Repair service" labour product. Resolved per request (a nested field gets
    its context only once bound, so the queryset can't be fixed in __init__)."""

    def get_queryset(self):
        from inventory.models import Product, ProductVariant
        store = getattr(getattr(self.context.get('request'), 'user', None), 'store', None)
        if store is None:
            return ProductVariant.all_objects.none()
        return (ProductVariant.all_objects
                .filter(store=store, is_deleted=False, product__is_deleted=False,
                        product__source=Product.Source.STORE)
                .exclude(product__product_type=Product.ProductType.SERVICE))


class ServicePartSerializer(serializers.ModelSerializer):
    """A part on a job. `unit_price` defaults to the variant's sell price. `unit_cost` (the variant's average
    cost) is only sent to roles that may see cost — never to a cashier by default."""
    id = serializers.UUIDField(required=False)
    variant = _ShopVariantField()
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal('0'))
    product_name = serializers.CharField(source='variant.product.name', read_only=True)
    sku = serializers.CharField(source='variant.sku', read_only=True)
    line_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    unit_cost = serializers.DecimalField(source='variant.cost_price', max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = ServicePart
        fields = ['id', 'variant', 'product_name', 'sku', 'quantity', 'unit_price', 'line_total', 'unit_cost']

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError('Quantity must be more than zero.')
        return value

    def validate_variant(self, value):
        # A serial-tracked product needs its unit's serial on the sale — the desk's Done can't ask for one, so it
        # would refuse the whole job (s169 batch review). Sell such a unit at the till instead.
        if value.product.track_serials:
            raise serializers.ValidationError(
                f'"{value.product.name}" tracks serial numbers — sell it at the till (with its serial), '
                'not as a repair part.', code='part_tracks_serials')
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if hides_cost(request_user(self)):
            data.pop('unit_cost', None)
        return data


class ServiceNoteSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = ServiceNote
        fields = ['id', 'body', 'user', 'user_name', 'created_at']
        read_only_fields = ['id', 'user', 'user_name', 'created_at']

    def get_user_name(self, obj):
        u = obj.user
        if u is None:
            return ''
        return (u.get_full_name() or u.username).strip()

    def validate_body(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Write the note first.')
        return value


# Fields that decide what Done charges. Once the job has its invoice they are frozen — editing them would
# make the job say one thing and the invoice another.
_BILLED_FIELDS = ('cost', 'detection_fee', 'is_warranty', 'parts')


class _ShopUserField(serializers.PrimaryKeyRelatedField):
    """A technician = an active user of the caller's shop; another shop's id is "not found"."""

    def get_queryset(self):
        from users.models import User
        store = getattr(getattr(self.context.get('request'), 'user', None), 'store', None)
        return User.objects.filter(store=store, is_active=True) if store is not None else User.objects.none()


class _ShopCustomerField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        from users.models import Customer
        store = getattr(getattr(self.context.get('request'), 'user', None), 'store', None)
        if store is None:
            return Customer.all_objects.none()
        return Customer.all_objects.filter(store=store, is_deleted=False)


def device_unit_info(unit):
    """What a job shows about the shop's own serial unit behind its device serial (read-only): the product,
    the sale it left on and its warranty. None when the serial is not one of the shop's units. No cost, no
    supplier — a cashier sees this."""
    from inventory.serials import warranty_active
    if unit is None:
        return None
    inv = unit.sale_item.invoice if unit.sale_item_id else None
    ends = unit.warranty_ends_on
    return {
        'id': str(unit.id),
        'serial': unit.serial,
        'status': unit.status,
        'product_id': str(unit.variant.product_id),
        'product_name': unit.variant.product.name,
        'sale': {'id': str(inv.id), 'invoice_number': inv.invoice_number, 'date': inv.date} if inv else None,
        'warranty_ends_on': ends,
        # The ONE rule shared with the Serial Lookup page (inventory.serials.warranty_active).
        'warranty_active': warranty_active(unit),
    }


class _ServiceListSerializer(serializers.ListSerializer):
    """The jobs list resolves every job's device serial in ONE query (not one lookup per row)."""

    def to_representation(self, data):
        from inventory.serials import find_serials
        rows = list(data.all() if hasattr(data, 'all') else data)
        store = getattr(request_user(self), 'store', None)
        self._context['device_units'] = find_serials(store, [r.device_serial for r in rows if r.device_serial])
        return super().to_representation(rows)


class ServiceSerializer(serializers.ModelSerializer):
    # Ids from the request body must belong to the caller's shop.
    technician = _ShopUserField(required=False, allow_null=True)
    client = _ShopCustomerField(required=False, allow_null=True)
    client_display_name = serializers.SerializerMethodField()
    client_display_phone = serializers.SerializerMethodField()
    eta_label = serializers.SerializerMethodField()
    technician_name = serializers.SerializerMethodField()
    parts = ServicePartSerializer(many=True, required=False)
    notes = serializers.SerializerMethodField()
    labour_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    parts_total = serializers.SerializerMethodField()
    invoice_number = serializers.SerializerMethodField()
    refund_number = serializers.SerializerMethodField()
    # The shop's serial unit the device serial names (product, sale, warranty) — null when it names none.
    device_unit = serializers.SerializerMethodField()

    class Meta:
        model = Service
        list_serializer_class = _ServiceListSerializer
        fields = [
            'id', 'serial_number', 'store',
            'client', 'client_name', 'client_phone',
            'client_display_name', 'client_display_phone',
            'service_type', 'receive_date',
            'no_eta', 'eta_days', 'eta_hours', 'eta_datetime',
            'info', 'keeping', 'cost', 'diagnosis',
            'technician', 'technician_name',
            'device', 'device_serial', 'device_unit', 'damage',
            'detection_fee', 'labour_total', 'parts', 'parts_total',
            'is_warranty', 'warranty_days',
            'notes',
            'status', 'notify_bell', 'notified',
            'invoice', 'invoice_number', 'refund', 'refund_number', 'created_by',
            'created_at', 'updated_at',
            'eta_label',
        ]
        read_only_fields = [
            'id', 'serial_number', 'store', 'created_by',
            'invoice', 'invoice_number', 'refund', 'refund_number', 'eta_datetime', 'notified',
            'client_display_name', 'client_display_phone', 'eta_label', 'technician_name',
            'labour_total', 'parts_total', 'notes', 'status', 'device_unit',
        ]

    # ── read helpers ──
    def get_client_display_name(self, obj):
        if obj.client_id:
            return obj.client.name
        return obj.client_name or ''

    def get_client_display_phone(self, obj):
        if obj.client_id:
            return obj.client.phone_number or ''
        return obj.client_phone or ''

    def get_technician_name(self, obj):
        u = obj.technician
        if u is None:
            return ''
        return (u.get_full_name() or u.username).strip()

    def get_notes(self, obj):
        notes = [n for n in obj.notes.all() if not n.is_deleted]
        return ServiceNoteSerializer(notes, many=True, context=self.context).data

    def get_parts_total(self, obj):
        total = sum((p.line_total for p in obj.parts.all() if not p.is_deleted), Decimal('0'))
        return str(total.quantize(Decimal('0.01')))

    def get_invoice_number(self, obj):
        return obj.invoice.invoice_number if obj.invoice_id else None

    def get_refund_number(self, obj):
        return obj.refund.refund_number if obj.refund_id else None

    def get_device_unit(self, obj):
        """Looked up in the CALLER's shop only — a job of another shop (never served anyway) gets nothing."""
        from inventory.serials import find_serial, normalize
        store = getattr(request_user(self), 'store', None)
        if not obj.device_serial or store is None or obj.store_id != store.id:
            return None
        units = self.context.get('device_units')
        unit = units.get(normalize(obj.device_serial)) if units is not None else find_serial(store, obj.device_serial)
        return device_unit_info(unit)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['parts'] = ServicePartSerializer(
            [p for p in instance.parts.all() if not p.is_deleted], many=True, context=self.context).data
        return data

    def get_eta_label(self, obj):
        if obj.no_eta or not obj.eta_datetime:
            return None
        from django.utils import timezone
        now = timezone.now()
        delta = obj.eta_datetime - now
        total_seconds = int(delta.total_seconds())
        if total_seconds <= 0:
            return 'overdue'
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    # ── validation ──
    def validate_detection_fee(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError('The detection fee cannot be negative.')
        return value

    def validate_cost(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError('The repair charge cannot be negative.')
        return value

    def validate(self, data):
        no_eta = data.get('no_eta', getattr(self.instance, 'no_eta', True))
        if not no_eta:
            eta_days = data.get('eta_days', getattr(self.instance, 'eta_days', None))
            eta_hours = data.get('eta_hours', getattr(self.instance, 'eta_hours', None))
            if not eta_days and not eta_hours:
                raise serializers.ValidationError(
                    {'eta_days': 'Provide at least days or hours when ETA is enabled.'}
                )
        inst = self.instance
        if inst is not None and (inst.invoice_id or inst.status != Service.Status.OPEN):
            changed = [f for f in _BILLED_FIELDS if f in data and (
                self._parts_differ(inst, data[f]) if f == 'parts' else data[f] != getattr(inst, f))]
            if changed:
                raise serializers.ValidationError(
                    {'detail': 'This job is no longer open — its charges and parts can no longer change.'})
        return data

    @staticmethod
    def _parts_differ(inst, rows):
        """True when the sent parts list is not the job's current one (a form re-sending it unchanged is fine)."""
        def key(variant_id, qty, price):
            return (str(variant_id), Decimal(str(qty)).normalize(),
                    None if price is None else Decimal(str(price)).normalize())
        now = sorted(key(p.variant_id, p.quantity, p.unit_price) for p in inst.parts.filter(is_deleted=False))
        sent = sorted(key(r['variant'].pk, r['quantity'],
                          r['unit_price'] if r.get('unit_price') is not None else r['variant'].sell_price)
                      for r in rows)
        return now != sent

    # ── writes ──
    def create(self, validated_data):
        parts = validated_data.pop('parts', None)
        with transaction.atomic():
            service = super().create(validated_data)
            if parts:
                self._save_parts(service, parts)
        return service

    def update(self, instance, validated_data):
        parts = validated_data.pop('parts', None)
        with transaction.atomic():
            service = super().update(instance, validated_data)
            if parts is not None:
                self._save_parts(service, parts)
        return service

    @staticmethod
    def _save_parts(service, parts):
        """The form sends the job's whole parts list. Rows sent with their id are updated, new rows added,
        rows left out are soft-deleted (never hard-deleted). No stock moves here — that happens at Done."""
        existing = {p.id: p for p in service.parts.filter(is_deleted=False)}
        keep = set()
        for row in parts:
            price = row.get('unit_price')
            if price is None:
                price = row['variant'].sell_price or Decimal('0')
            pid = row.get('id')
            part = existing.get(pid) if pid else None
            if part is not None:
                part.variant, part.quantity, part.unit_price = row['variant'], row['quantity'], price
                part.save(update_fields=['variant', 'quantity', 'unit_price', 'updated_at'])
                keep.add(part.id)
            else:
                ServicePart.objects.create(store=service.store, service=service, variant=row['variant'],
                                           quantity=row['quantity'], unit_price=price)
        for pid, part in existing.items():
            if pid not in keep:
                part.delete()   # soft delete
