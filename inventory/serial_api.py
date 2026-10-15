"""Serial lookup + manager tools API (PROLINE-PARITY New 7/8) — /api/inventory/serials/.

GET  /serials/?q=&product=&variant=&branch=&status=   search (≤ 200 rows) — CASHIER (warranty check at the counter)
GET  /serials/<id>/                                    one unit + its full history — CASHIER
POST /serials/register/   {variant, branch, serial_numbers, note?}   serials for units already on hand — MANAGER
POST /serials/<id>/remove/ {note?}                     take back a wrongly typed serial (IN_STOCK only) — MANAGER

Every query is scoped to request.user.store; ids from the body are checked to belong to it. The supplier
follows the shop's "hide supplier" switch for the caller's role; no cost is ever sent.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers, viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.field_visibility import hides_supplier, request_user
from core.models import Branch
from users.permissions import RoleScopedPermission

from . import serials as serial_engine
from .models import ProductVariant, SerialUnit, SerialMove


def _by_id(qs, value):
    """The row of `qs` (already scoped to the caller's shop) with this id, or None — a malformed id too."""
    if not value:
        return None
    try:
        return qs.filter(id=value).first()
    except (DjangoValidationError, ValueError):
        return None


def _purchase_info(pi, show_supplier):
    if pi is None:
        return None
    inv = pi.invoice
    return {
        'id': str(inv.id),
        'purchase_number': inv.purchase_number or '',
        'vendor_reference': inv.vendor_reference or '',
        'date': inv.date,
        'supplier_name': (inv.supplier.name if inv.supplier_id else None) if show_supplier else None,
    }


def _sale_info(si):
    if si is None:
        return None
    inv = si.invoice
    return {
        'id': str(inv.id),
        'invoice_number': inv.invoice_number,
        'date': inv.date,
        'status': inv.status,
        'customer_name': inv.customer.name if inv.customer_id else None,
    }


class SerialUnitSerializer(serializers.ModelSerializer):
    product_id = serializers.CharField(source='variant.product_id', read_only=True)
    product_name = serializers.CharField(source='variant.product.name', read_only=True)
    sku = serializers.CharField(source='variant.sku', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)
    condition = serializers.SerializerMethodField()
    warranty_active = serializers.SerializerMethodField()
    purchase = serializers.SerializerMethodField()
    sale = serializers.SerializerMethodField()

    class Meta:
        model = SerialUnit
        fields = ['id', 'serial', 'status', 'product_id', 'product_name', 'variant', 'sku',
                  'branch', 'branch_name', 'condition', 'sold_at', 'warranty_months', 'warranty_ends_on',
                  'warranty_active', 'purchase', 'sale', 'created_at']

    def _show_supplier(self):
        return not hides_supplier(request_user(self))

    def get_condition(self, obj):
        """As it was sold (frozen on the sale line) — else the product's condition today."""
        if obj.sale_item_id:
            return obj.sale_item.condition or ''
        return obj.variant.product.condition or ''

    def get_warranty_active(self, obj):
        from .serials import warranty_active
        return warranty_active(obj)

    def get_purchase(self, obj):
        return _purchase_info(obj.purchase_item, self._show_supplier())

    def get_sale(self, obj):
        return _sale_info(obj.sale_item)


class SerialUnitDetailSerializer(SerialUnitSerializer):
    moves = serializers.SerializerMethodField()

    class Meta(SerialUnitSerializer.Meta):
        fields = SerialUnitSerializer.Meta.fields + ['moves']

    def get_moves(self, obj):
        show = self._show_supplier()
        out = []
        for m in (obj.moves.select_related(
                'branch', 'to_branch', 'created_by',
                'purchase_item__invoice__supplier', 'sale_item__invoice__customer',
                'refund_item__refund', 'adjustment', 'transfer').order_by('created_at')):
            out.append({
                'kind': m.kind,
                'at': m.created_at,
                'branch_name': m.branch.name if m.branch_id else None,
                'to_branch_name': m.to_branch.name if m.to_branch_id else None,
                'purchase': _purchase_info(m.purchase_item, show),
                'sale': _sale_info(m.sale_item),
                'refund_number': m.refund_item.refund.refund_number if m.refund_item_id else None,
                'adjustment_reason': m.adjustment.reason if m.adjustment_id else None,
                'note': m.note,
                'by': (m.created_by.get_full_name() or m.created_by.username) if m.created_by_id else None,
            })
        return out


class SerialUnitViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':     'CASHIER',
        'retrieve': 'CASHIER',
        'register': 'MANAGER',
        'remove':   'MANAGER',
    }
    MAX_ROWS = 200

    def get_serializer_class(self):
        return SerialUnitDetailSerializer if self.action == 'retrieve' else SerialUnitSerializer

    def get_queryset(self):
        qs = (SerialUnit.objects.filter(store=self.request.user.store, is_deleted=False)
              .select_related('variant__product', 'branch',
                              'purchase_item__invoice__supplier', 'sale_item__invoice__customer'))
        p = self.request.query_params
        if q := serial_engine.normalize(p.get('q')):
            qs = qs.filter(serial__icontains=q)
        if prod := p.get('product'):
            qs = qs.filter(variant__product_id=prod)
        if var := p.get('variant'):
            qs = qs.filter(variant_id=var)
        if br := p.get('branch'):
            qs = qs.filter(branch_id=br)
        if st := p.get('status'):
            qs = qs.filter(status=st)
        return qs.order_by('serial', '-created_at')

    def list(self, request, *args, **kwargs):
        try:
            rows = list(self.get_queryset()[:self.MAX_ROWS + 1])
        except (DjangoValidationError, ValueError):   # a malformed uuid in a filter → empty, not a 500
            rows = []
        data = self.get_serializer(rows[:self.MAX_ROWS], many=True).data
        return Response({'results': data, 'truncated': len(rows) > self.MAX_ROWS})

    @action(detail=False, methods=['post'])
    def register(self, request):
        store = request.user.store
        variant = _by_id(ProductVariant.objects.filter(product__store=store, product__is_deleted=False),
                         request.data.get('variant'))
        branch = _by_id(Branch.objects.filter(store=store), request.data.get('branch'))
        if variant is None:
            raise ValidationError({'variant': 'Product not found in this shop.'})
        if branch is None:
            raise ValidationError({'branch': 'Branch not found in this shop.'})
        with transaction.atomic():
            units = serial_engine.register(store, variant, branch, request.data.get('serial_numbers'),
                                           user=request.user, note=request.data.get('note') or '')
        return Response({'created': len(units),
                         'results': SerialUnitSerializer(units, many=True, context={'request': request}).data},
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def remove(self, request, pk=None):
        unit = self.get_object()
        with transaction.atomic():
            serial_engine.remove(unit, user=request.user, note=request.data.get('note') or '')
        return Response(status=status.HTTP_204_NO_CONTENT)
