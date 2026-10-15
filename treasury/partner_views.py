"""Owner level API — part B (TREASURY.md § 12.4): partners, settlements, business P&L.
Built against the contract; every view inherits `owner_base.OwnerMixin` (OWNER role + both switches + the login IS the
owner; data scoped by `self.owner`, another owner's row = 404). Money lines and the settlement maths live in
`treasury.ledger` — these views only check, call and shape.

Privacy: nothing here is written to the shop's activity log — shop managers read that log, and partner names, amounts
and personal boxes are the owner's alone. The money lines (created_by) and the settlements (created_by) are the trail.
"""
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from . import ledger
from .ledger import TreasuryError, ZERO
from .models import MoneyMove, OwnerPart, PartEntry, Partner, PartnerSettlement, PartnerSettlementLine
from .owner_base import OwnerMixin
from .partner_serializers import (
    PartnerSerializer, PartnerMoneySerializer, SettlementCreateSerializer, CancelSerializer, settlement_data,
    preview_data,
)
from .serializers import money
from .owner_serializers import owner_move_line

K = MoneyMove.Kind
OWNER_ONLY = {'list': 'OWNER', 'retrieve': 'OWNER', 'create': 'OWNER', 'partial_update': 'OWNER'}


def _date(params, key, required=False):
    """A YYYY-MM-DD query param → date. Bad / impossible date → 400 `{key: …}` (same words as the Wave 1 statement)."""
    raw = (params.get(key) or '').strip()
    if not raw:
        if required:
            raise ValidationError({key: 'Choose a date.'})
        return None
    try:
        day = parse_date(raw)
    except ValueError:   # well-formed but impossible, e.g. 2026-02-31
        day = None
    if day is None:
        raise ValidationError({key: 'Use a date like 2026-01-31.'})
    return day


def _range(params, required=False):
    df, dt = _date(params, 'date_from', required), _date(params, 'date_to', required)
    if df and dt and df > dt:
        raise ValidationError({'date_to': 'The end date is before the start date.'})
    return df, dt


def _uuid(raw, key):
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        raise ValidationError({key: 'Choose one from the list.'})


class _PartMixin(OwnerMixin):
    def part_from_param(self, key='part', required=True):
        raw = (self.request.query_params.get(key) or '').strip()
        if not raw:
            if required:
                raise ValidationError({key: 'Choose a part.'})
            return None
        return self.owner_part(_uuid(raw, key))


# ── partners ────────────────────────────────────────────────────────────────────────────────────────────────────
class PartnerViewSet(_PartMixin, viewsets.GenericViewSet):
    """Partners of the owner's SHOP / BUSINESS parts. `?part=` filters the list. Shares: Σ active ≤ 100 per part
    (`ledger.check_shares`, the part row locked); deactivating is always allowed. The partner's `account` = what the
    business owes them (settled shares + put in − paid)."""
    serializer_class = PartnerSerializer
    http_method_names = ['get', 'post', 'patch', 'head', 'options']
    role_map = {**OWNER_ONLY, 'put_in': 'OWNER', 'pay': 'OWNER', 'statement': 'OWNER'}

    def get_queryset(self):
        qs = (Partner.objects.filter(owner=self.owner, part__in=ledger.owner_parts(self.owner))
              .select_related('part').order_by('-is_active', 'name', 'id'))
        if self.action == 'list':
            part = self.part_from_param(required=False)
            if part is not None:
                qs = qs.filter(part=part)
        return qs

    def list(self, request):
        return Response(self.get_serializer(self.get_queryset(), many=True).data)

    def retrieve(self, request, pk=None):
        return Response(self.get_serializer(self.get_object()).data)

    def create(self, request):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        with transaction.atomic():
            part = self.owner_part(d.pop('part_id'))
            # Also refuses a PERSONAL part (partners_business_only).
            ledger.check_shares(part, d['share_pct'] if d.get('is_active', True) else ZERO)
            partner = Partner.objects.create(owner=self.owner, part=part, **d)
        return Response(self.get_serializer(partner).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        with transaction.atomic():
            partner = self.get_object()
            ser = self.get_serializer(partner, data=request.data, partial=True)
            ser.is_valid(raise_exception=True)
            d = ser.validated_data
            if 'part_id' in d and d['part_id'] != partner.part_id:
                raise ValidationError({'part': 'A partner stays in their part — add them again in the other one.'})
            d.pop('part_id', None)
            pct = d.get('share_pct', partner.share_pct)
            active = d.get('is_active', partner.is_active)
            # A raise of the share or a come-back must fit under 100 %; lowering or leaving never needs a check.
            if active and (not partner.is_active or pct > partner.share_pct):
                ledger.check_shares(partner.part, pct, exclude=partner)
            ser.save()
        return Response(self.get_serializer(partner).data)

    def _money(self, request, door):
        partner = self.get_object()
        ser = PartnerMoneySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        box = self.owner_box(d['box'])
        with transaction.atomic():
            line = door(partner, d['amount'], box, user=request.user, note=d['note'])
        return Response({'move': owner_move_line(line), 'account': money(ledger.partner_account(partner))},
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='put-in')
    def put_in(self, request, pk=None):
        """POST partners/{id}/put-in/ {amount, box, note?} — the partner put money in (a box of the partner's part)."""
        return self._money(request, ledger.partner_put_in)

    @action(detail=True, methods=['post'])
    def pay(self, request, pk=None):
        """POST partners/{id}/pay/ {amount, box, note?} — the business paid the partner (from a box of their part)."""
        return self._money(request, ledger.partner_pay)

    @action(detail=True, methods=['get'])
    def statement(self, request, pk=None):
        """GET partners/{id}/statement/ — the partner's account oldest first: their share of every live settlement
        (SETTLEMENT, on the period's last day) + the money they put in (PUT_IN) / were paid (PAID) + reversals of
        those (REVERSAL), each with the running account. The last running = `account`."""
        partner = self.get_object()
        rows = []
        for l in (PartnerSettlementLine.objects.filter(partner=partner, settlement__cancelled_at__isnull=True)
                  .select_related('settlement')):
            s = l.settlement
            rows.append(((s.date_to, s.created_at), {
                'date': s.date_to, 'at': None, 'kind': 'SETTLEMENT', 'amount': l.amount,
                'ref': {'type': 'settlement', 'id': str(s.id), 'date_from': s.date_from, 'date_to': s.date_to,
                        'share_pct': money(l.share_pct)},
                'note': '', 'reversed': False}))
        reversed_ids = set(MoneyMove.all_objects.filter(partner=partner, reverses__isnull=False)
                           .values_list('reverses_id', flat=True))
        kinds = {K.PARTNER_IN: 'PUT_IN', K.PARTNER_OUT: 'PAID', K.REVERSAL: 'REVERSAL'}
        for m in MoneyMove.all_objects.filter(partner=partner).select_related('box'):
            day = timezone.localdate(m.at)
            rows.append(((day, m.at), {
                'date': day, 'at': m.at.isoformat(), 'kind': kinds.get(m.kind, m.kind), 'amount': m.amount,
                'ref': {'type': 'move', 'id': str(m.id), 'box': str(m.box_id), 'box_name': m.box.name,
                        'reverses': str(m.reverses_id) if m.reverses_id else None},
                'note': m.note or m.reason, 'reversed': m.id in reversed_ids}))
        rows.sort(key=lambda r: r[0])
        running, lines = ZERO, []
        for _key, row in rows:
            running += row['amount']
            lines.append({**row, 'amount': money(row['amount']), 'running': money(running)})
        return Response({'partner': self.get_serializer(partner).data, 'lines': lines, 'account': money(running)})


# ── settlements ─────────────────────────────────────────────────────────────────────────────────────────────────
class SettlementViewSet(_PartMixin, viewsets.GenericViewSet):
    """Profit splits per period. Create = `ledger.settle` (figures frozen); cancel = a mark with a reason (never a
    delete) — the period is free again and its lines leave every partner's account. Preview = the same maths, saved
    nowhere."""
    http_method_names = ['get', 'post', 'head', 'options']
    role_map = {**OWNER_ONLY, 'preview': 'OWNER', 'cancel': 'OWNER'}

    def get_queryset(self):
        qs = (PartnerSettlement.objects.filter(owner=self.owner, part__in=ledger.owner_parts(self.owner))
              .select_related('part', 'created_by').prefetch_related('lines__partner'))
        if self.action == 'list':
            part = self.part_from_param(required=False)
            if part is not None:
                qs = qs.filter(part=part)
        return qs

    def list(self, request):
        return Response([settlement_data(s) for s in self.get_queryset()])

    def retrieve(self, request, pk=None):
        return Response(settlement_data(self.get_object()))

    def create(self, request):
        ser = SettlementCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        part = self.owner_part(d['part'])
        if d['date_from'] > d['date_to']:
            raise ValidationError({'date_to': 'The end date is before the start date.'})
        with transaction.atomic():
            s = ledger.settle(part, d['date_from'], d['date_to'], user=request.user)
        return Response(settlement_data(self.get_queryset().get(pk=s.pk)), status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def preview(self, request):
        """GET settlements/preview/?part&date_from&date_to — the split the owner would save, nothing written."""
        part = self.part_from_param()
        df, dt = _range(request.query_params, required=True)
        return Response(preview_data(part, df, dt, ledger.settlement_preview(part, df, dt)))

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """POST settlements/{id}/cancel/ {reason}."""
        s = self.get_object()
        ser = CancelSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            ledger.cancel_settlement(s, user=request.user, reason=ser.validated_data['reason'])
        return Response(settlement_data(self.get_queryset().get(pk=s.pk)))


# ── profit & loss per business ──────────────────────────────────────────────────────────────────────────────────
class PartPnlViewSet(_PartMixin, viewsets.ViewSet):
    """GET pnl/?part=&date_from&date_to[&granularity] (default: the last 30 days, like /api/reports/pnl/).
    SHOP → the shop P&L (`compute_pnl` — the very same maths as the report) + owner-only expenses; another shop of the
    owner → 400 `switch_shop`. BUSINESS → income / expenses / net + by category, from its live entries.
    PERSONAL → 400 `personal_no_pnl` (personal money is never profit)."""
    role_map = {'list': 'OWNER'}

    def list(self, request):
        part = self.part_from_param()
        df, dt = _range(request.query_params)
        today = timezone.localdate()
        df = df or (dt or today) - timedelta(days=30)
        dt = dt or today
        if df > dt:
            raise ValidationError({'date_to': 'The end date is before the start date.'})
        head = {'part': str(part.id), 'part_kind': part.kind, 'part_name': part.name, 'date_from': df, 'date_to': dt}

        if part.kind == OwnerPart.Kind.PERSONAL:
            raise TreasuryError('personal_no_pnl', 'Personal money has no profit and loss.')
        fig = ledger.part_profit(part, df, dt)   # SHOP of another shop → switch_shop (before any figure is read)
        if part.kind == OwnerPart.Kind.SHOP:
            from reports.views import compute_pnl
            g = request.query_params.get('granularity', 'month')
            g = g if g in ('day', 'week', 'month') else 'month'
            rows, totals = compute_pnl(part.store, df, dt, g)
            return Response({**head, 'granularity': g, 'rows': rows, 'totals': totals,
                             'owner_only_expenses': money(fig['owner_only_expenses'])})

        live = PartEntry.objects.filter(owner=self.owner, part=part, date__gte=df, date__lte=dt)
        by_cat = (live.values('direction', 'category').annotate(amount=Sum('amount'))
                  .order_by('direction', '-amount', 'category'))
        income = sum((r['amount'] for r in by_cat if r['direction'] == PartEntry.Direction.IN), ZERO)
        expenses = sum((r['amount'] for r in by_cat if r['direction'] == PartEntry.Direction.OUT), ZERO)
        return Response({**head, 'income': money(income), 'expenses': money(expenses), 'net': money(income - expenses),
                         'owner_only_expenses': money(fig['owner_only_expenses']),
                         'by_category': [{'direction': r['direction'], 'category': r['category'],
                                          'amount': money(r['amount'])} for r in by_cat]})
