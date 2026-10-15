"""Payment due dates API (s173, PAYMENT-DUES.md). Every change goes through finance/payment_dues.py (one door).

GET  /api/finance/payment-dues/?status=OPEN|DONE|CANCELLED &due=late|today|soon &customer= &plan= &q=
POST /api/finance/payment-dues/ {customer, amount, first_date, count?=1, every?=1, unit?=MONTH|WEEK|DAY, note?, invoice?}
     → the list of due dates made (one, or the instalments)                      (needs the shop switch on)
PATCH {id}/ {due_date?, amount?, note?}                                           open ones only
POST {id}/done/   {receipt?, reason?}   paid = the customer receipt that brought the money; no receipt → a reason
POST {id}/cancel/ {reason}
GET  summary/     late / today / soon (7 days): counts + totals — the reminder banner
Debts are a credit decision → MANAGER and up, like cheques and customer receipts.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone as tz
from django.utils.dateparse import parse_date
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.activity import log_activity
from core.models import ActivityLog
from users.models import Customer
from users.permissions import RoleScopedPermission
from . import payment_dues as svc
from .models import CustomerReceipt, PaymentDue, SalesInvoice
from .views import _is_uuid


class PaymentDueSerializer(serializers.ModelSerializer):
    """Read shape. Writes go through PaymentDueViewSet → finance/payment_dues.py."""
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_phone = serializers.CharField(source='customer.phone_number', read_only=True)
    invoice_number = serializers.IntegerField(source='invoice.invoice_number', read_only=True, default=None)
    created_by_name = serializers.SerializerMethodField()
    days_to_due = serializers.SerializerMethodField()

    class Meta:
        model = PaymentDue
        fields = ['id', 'customer', 'customer_name', 'customer_phone', 'amount', 'due_date', 'days_to_due', 'status',
                  'plan', 'plan_seq', 'plan_count', 'invoice', 'invoice_number', 'note', 'done_on', 'receipt',
                  'status_reason', 'created_by_name', 'created_at']
        read_only_fields = fields

    def get_created_by_name(self, obj):
        u = obj.created_by
        return (u.get_full_name() or u.username) if u else ''

    def get_days_to_due(self, obj):
        return (obj.due_date - tz.localdate()).days


class PaymentDueViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PaymentDueSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list': 'MANAGER', 'retrieve': 'MANAGER', 'create': 'MANAGER', 'partial_update': 'MANAGER',
        'done': 'MANAGER', 'cancel': 'MANAGER', 'summary': 'MANAGER',
    }

    def get_queryset(self):
        qs = (PaymentDue.objects.filter(store=self.request.user.store)
              .select_related('customer', 'invoice', 'created_by'))
        p = self.request.query_params
        if p.get('status'):
            qs = qs.filter(status=p['status'])
        for key in ('customer', 'plan'):
            if p.get(key):
                if not _is_uuid(p[key]):
                    return qs.none()
                qs = qs.filter(**{key + ('_id' if key == 'customer' else ''): p[key]})
        today = tz.localdate()
        due = p.get('due')
        if due in ('late', 'today', 'soon'):
            qs = qs.filter(status=PaymentDue.Status.OPEN)
            if due == 'late':
                qs = qs.filter(due_date__lt=today)
            elif due == 'today':
                qs = qs.filter(due_date=today)
            else:
                qs = qs.filter(due_date__gt=today, due_date__lte=today + timedelta(days=svc.SOON_DAYS))
        if p.get('q'):
            q = p['q'].strip()
            qs = qs.filter(Q(customer__name__icontains=q) | Q(customer__phone_number__icontains=q) | Q(note__icontains=q))
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
        raw = raw.strip() if isinstance(raw, str) else ''
        if not raw:
            return None
        try:
            d = parse_date(raw)
        except ValueError:
            d = None
        if d is None:
            raise ValidationError({key: 'Use a date like 2026-01-31.'})
        return d

    def create(self, request, *args, **kwargs):
        store, d = request.user.store, request.data
        dues = svc.create_dues(
            store, request.user, customer=self._pick(Customer, 'customer', store=store),
            amount=d.get('amount'), first_date=self._date('first_date'), count=d.get('count'),
            every=d.get('every'), unit=d.get('unit') or 'MONTH', note=d.get('note') or '',
            invoice=self._pick(SalesInvoice, 'invoice', store=store))
        first = dues[0]
        what = f'{len(dues)} instalments' if len(dues) > 1 else 'a payment due date'
        log_activity(request=request, action=f"Set {what} for {first.customer.name}: {sum(x.amount for x in dues)}",
                     op_type=ActivityLog.OperationType.OTHER,
                     details={'payment_due_ids': [str(x.id) for x in dues]})
        return Response(self.get_serializer(dues, many=True).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        d = request.data
        due = svc.update_due(self.get_object(), request.user, due_date=self._date('due_date'),
                             amount=d.get('amount') if d.get('amount') not in (None, '') else None,
                             note=d.get('note') if isinstance(d.get('note'), str) else None)
        return Response(self.get_serializer(due).data)

    @action(detail=True, methods=['post'])
    def done(self, request, pk=None):
        due = self.get_object()
        due = svc.mark_done(due, request.user, reason=request.data.get('reason') or '',
                            receipt=self._pick(CustomerReceipt, 'receipt', store=request.user.store))
        log_activity(request=request, action=f"Payment due date closed: {due.amount} ({due.customer.name})",
                     op_type=ActivityLog.OperationType.OTHER, details={'payment_due_id': str(due.id)})
        return Response(self.get_serializer(due).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        due = svc.cancel_due(self.get_object(), request.user, request.data.get('reason') or '')
        log_activity(request=request, action=f"Payment due date cancelled: {due.amount} ({due.customer.name})",
                     op_type=ActivityLog.OperationType.OTHER,
                     details={'payment_due_id': str(due.id), 'reason': due.status_reason})
        return Response(self.get_serializer(due).data)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        return Response(svc.summary(request.user.store))
