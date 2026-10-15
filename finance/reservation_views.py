"""Reservations API (s172, RESERVATIONS.md). Every state change goes through finance/reservations.py (one door).

GET  /api/finance/reservations/?status=OPEN|CONVERTED|CANCELLED &overdue=1 &customer= &q=
POST /api/finance/reservations/ {branch, customer, items:[{variant, quantity, unit_price, serial_numbers?}], hold_until?,
                                 note?, salesperson?, deposit_amount?, deposit_method?}   (needs the shop switch on)
PATCH {id}/ {hold_until?, note?}
POST {id}/deposit/   {amount, method, note?}
POST {id}/give-back/ {amount, method, box?, note?}                     MANAGER
POST {id}/convert/   {payments?:[…the till's lines…], serials?: {item id: [serial…]}}  (+ Idempotency-Key)
POST {id}/cancel/    {outcome: REFUNDED|KEPT|TO_CREDIT, reason, method?, box?}   KEPT → MANAGER
GET  held/?variant=&branch=   units held by open reservations (for the stock screens)
"""
from django.db.models import Q
from django.utils import timezone as tz
from django.utils.dateparse import parse_date
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.activity import log_activity
from core.models import ActivityLog, Branch
from users.models import Customer, User
from users.permissions import ROLE_RANK, RoleScopedPermission, _user_rank
from . import reservations as svc
from .models import PaymentMethod, Reservation, ReservationDeposit, ReservationItem
from .views import _is_uuid


def _name(u):
    return (u.get_full_name() or u.username) if u else ''


class ReservationItemSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(source='variant.sku', read_only=True)
    name = serializers.CharField(source='variant.product.name', read_only=True)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    track_serials = serializers.BooleanField(source='variant.product.track_serials', read_only=True)

    class Meta:
        model = ReservationItem
        fields = ['id', 'variant', 'sku', 'name', 'quantity', 'unit_price', 'total', 'serial_numbers', 'track_serials']
        read_only_fields = fields


class ReservationDepositSerializer(serializers.ModelSerializer):
    method_name = serializers.CharField(source='method.name', read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ReservationDeposit
        fields = ['id', 'amount', 'method', 'method_name', 'created_at', 'created_by_name', 'note']
        read_only_fields = fields

    def get_created_by_name(self, obj):
        return _name(obj.created_by)


class ReservationSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_phone = serializers.CharField(source='customer.phone_number', read_only=True, default='')
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    items = ReservationItemSerializer(many=True, read_only=True)
    deposits = ReservationDepositSerializer(many=True, read_only=True)
    deposit_held = serializers.SerializerMethodField()
    still_due = serializers.SerializerMethodField()
    overdue = serializers.SerializerMethodField()
    sale_number = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    cancelled_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Reservation
        fields = ['id', 'number', 'status', 'customer', 'customer_name', 'customer_phone', 'branch', 'branch_name',
                  'date', 'hold_until', 'overdue', 'note', 'total', 'deposit_held', 'still_due', 'salesperson_name',
                  'created_by_name', 'sale', 'sale_number', 'converted_at', 'converted_extra_to_credit',
                  'cancelled_at', 'cancelled_by_name', 'cancel_reason', 'deposit_outcome', 'kept_amount',
                  'items', 'deposits']
        read_only_fields = fields

    def _held(self, obj):
        if not hasattr(obj, '_held_cache'):
            obj._held_cache = sum((d.amount for d in obj.deposits.all()), start=svc.ZERO)
        return obj._held_cache

    def get_deposit_held(self, obj):
        return str(self._held(obj))

    def get_still_due(self, obj):
        if obj.status != Reservation.Status.OPEN:
            return '0.00'
        return str(max(obj.total - self._held(obj), svc.ZERO))

    def get_overdue(self, obj):
        return bool(obj.status == Reservation.Status.OPEN and obj.hold_until and obj.hold_until < tz.localdate())

    def get_sale_number(self, obj):
        return obj.sale.invoice_number if obj.sale_id else None

    def get_created_by_name(self, obj):
        return _name(obj.created_by)

    def get_cancelled_by_name(self, obj):
        return _name(obj.cancelled_by)


class ReservationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ReservationSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list': 'CASHIER', 'retrieve': 'CASHIER', 'create': 'CASHIER', 'partial_update': 'CASHIER',
        'deposit': 'CASHIER', 'convert': 'CASHIER', 'cancel': 'CASHIER', 'give_back': 'MANAGER', 'held': 'CASHIER',
    }

    def get_queryset(self):
        qs = (Reservation.objects.filter(store=self.request.user.store)
              .select_related('customer', 'branch', 'sale', 'created_by', 'cancelled_by')
              .prefetch_related('items__variant__product', 'deposits__method', 'deposits__created_by'))
        p = self.request.query_params
        if p.get('status'):
            qs = qs.filter(status=p['status'])
        if p.get('overdue') in ('1', 'true'):
            qs = qs.filter(status=Reservation.Status.OPEN, hold_until__lt=tz.localdate())
        if p.get('customer'):
            if not _is_uuid(p['customer']):
                return qs.none()
            qs = qs.filter(customer_id=p['customer'])
        q = (p.get('q') or '').strip()
        if q:
            cond = Q(customer__name__icontains=q) | Q(customer__phone_number__icontains=q) | Q(
                items__variant__product__name__icontains=q) | Q(items__variant__sku__icontains=q)
            if q.isdigit():
                cond |= Q(number=int(q))
            qs = qs.filter(cond).distinct()
        return qs

    def get_object(self):
        if not _is_uuid(self.kwargs.get('pk')):
            from django.http import Http404
            raise Http404
        return super().get_object()

    def _pick(self, model, key, **scope):
        val = self.request.data.get(key)
        if not val:
            return None
        obj = model.all_objects.filter(pk=val, **scope).first() if _is_uuid(val) else None
        if obj is None:
            raise ValidationError({key: 'Not found.'})
        return obj

    def _date(self, key):
        raw = self.request.data.get(key)
        if raw in (None, ''):
            return None
        d = parse_date(raw) if isinstance(raw, str) else None
        if d is None:
            raise ValidationError({key: 'Use a date like 2026-01-31.'})
        return d

    def _box(self, store):
        from treasury.models import MoneyBox
        val = self.request.data.get('box')
        if not val:
            return None
        box = MoneyBox.all_objects.filter(pk=val, store=store).first() if _is_uuid(val) else None
        if box is None:
            raise ValidationError({'box': 'Not found.'})
        return box

    def _out(self, res, code=status.HTTP_200_OK):
        return Response(self.get_serializer(self.get_queryset().get(pk=res.pk)).data, status=code)

    def create(self, request, *args, **kwargs):
        store = request.user.store
        salesperson = None
        if request.data.get('salesperson'):
            val = request.data['salesperson']
            salesperson = User.objects.filter(pk=val, store=store, is_active=True).first() if str(val).isdigit() or _is_uuid(val) else None
            if salesperson is None:
                raise ValidationError({'salesperson': 'Not found.'})
        res = svc.create_reservation(
            store, request.user, branch=self._pick(Branch, 'branch', store=store),
            customer=self._pick(Customer, 'customer', store=store), items=request.data.get('items'),
            hold_until=self._date('hold_until'), note=request.data.get('note') or '', salesperson=salesperson,
            deposit_amount=request.data.get('deposit_amount'),
            deposit_method=self._pick(PaymentMethod, 'deposit_method', store=store))
        log_activity(request=request, action=f"Reservation {res.number} for {res.customer.name}: {res.total}",
                     op_type=ActivityLog.OperationType.SALE, details={'reservation_id': str(res.id)})
        return self._out(res, status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        res = self.get_object()
        kw = {}
        if 'hold_until' in request.data:
            kw['hold_until'] = self._date('hold_until')
        if 'note' in request.data:
            kw['note'] = request.data.get('note') or ''
        svc.update_reservation(res, request.user, **kw)
        return self._out(res)

    @action(detail=True, methods=['post'])
    def deposit(self, request, pk=None):
        res = self.get_object()
        dep = svc.take_deposit(res, request.user, amount=request.data.get('amount'),
                               method=self._pick(PaymentMethod, 'method', store=request.user.store),
                               note=request.data.get('note') or '')
        log_activity(request=request, action=f"Deposit {dep.amount} on reservation {res.number}",
                     op_type=ActivityLog.OperationType.SALE, details={'reservation_id': str(res.id)})
        return self._out(res)

    @action(detail=True, methods=['post'], url_path='give-back')
    def give_back(self, request, pk=None):
        res = self.get_object()
        store = request.user.store
        dep = svc.give_back(res, request.user, amount=request.data.get('amount'),
                            method=self._pick(PaymentMethod, 'method', store=store), box=self._box(store),
                            note=request.data.get('note') or '')
        log_activity(request=request, action=f"Gave back {-dep.amount} of reservation {res.number}'s deposit",
                     op_type=ActivityLog.OperationType.SALE, details={'reservation_id': str(res.id)})
        return self._out(res)

    @action(detail=True, methods=['post'])
    def convert(self, request, pk=None):
        from .serializers import SalesInvoiceSerializer
        res = self.get_object()
        idem_key = (request.headers.get('Idempotency-Key') or '').strip()[:64]
        if res.status == Reservation.Status.CONVERTED and res.sale_id and idem_key and res.sale.checkout_key == idem_key:
            return Response(SalesInvoiceSerializer(res.sale).data)   # a retry of a conversion that went through
        invoice = svc.convert(res, request.user, request.data, idem_key)
        log_activity(request=request, action=f"Reservation {res.number} became sale #{invoice.invoice_number}",
                     op_type=ActivityLog.OperationType.SALE,
                     details={'reservation_id': str(res.id), 'invoice_id': str(invoice.id)})
        return Response(SalesInvoiceSerializer(invoice).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        res = self.get_object()
        outcome = request.data.get('outcome') or ''
        if outcome == Reservation.DepositOutcome.KEPT and _user_rank(request.user) < ROLE_RANK[User.Role.MANAGER]:
            raise PermissionDenied('Only a manager can keep a deposit.')
        store = request.user.store
        svc.cancel(res, request.user, outcome=outcome, reason=request.data.get('reason') or '',
                   method=self._pick(PaymentMethod, 'method', store=store), box=self._box(store))
        log_activity(request=request, action=f"Cancelled reservation {res.number}",
                     op_type=ActivityLog.OperationType.SALE, details={'reservation_id': str(res.id)})
        return self._out(res)

    @action(detail=False, methods=['get'])
    def held(self, request):
        """{variant_id: units held} by open reservations (optionally one branch / one variant)."""
        from django.db.models import Sum
        qs = ReservationItem.objects.filter(reservation__store=request.user.store,
                                            reservation__status=Reservation.Status.OPEN,
                                            reservation__is_deleted=False)
        for key, field in (('branch', 'reservation__branch_id'), ('variant', 'variant_id')):
            val = request.query_params.get(key)
            if val:
                if not _is_uuid(val):
                    return Response({})
                qs = qs.filter(**{field: val})
        rows = qs.values('variant_id').annotate(q=Sum('quantity'))
        return Response({str(r['variant_id']): str(r['q']) for r in rows})
