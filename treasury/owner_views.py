"""Owner level API — part A (TREASURY.md § 12.3): overview, parts, owner boxes, moving money, entries, loans, amanat.

Every view inherits `owner_base.OwnerMixin`: OWNER role (sudo refused) → both switches on (403 `owner_level_off`) → the
login IS the owner (403 `not_owner`). Every lookup is scoped by `self.owner` (another owner's row = 404) — never by
`request.user.store` alone: business and personal rows have no shop. Every money line goes through `treasury.ledger`
inside a transaction; a refused move (`TreasuryError`) answers 400 `{"code", "detail", **params}`.

Activity log: the shop's log (MANAGER+ read it) gets a line ONLY when a SHOP box is touched — generic words, the shop
box and the amount (staff already see that line in the shop statement). Personal / business events never reach a shop's
log: they are not shop events, and the person's private money is nobody's business.
"""
from django.db import transaction
from django.db.models import Case, IntegerField, Sum, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from core.activity import log_activity
from core.models import ActivityLog, Branch

from . import ledger
from .ledger import TreasuryError, ZERO
from .models import OwnerPart, MoneyBox, MoneyMove, PartEntry, Loan, Amana
from .owner_base import OwnerMixin
from .owner_serializers import (
    OwnerPartSerializer, OwnerPartCreateSerializer, OwnerPartPatchSerializer, OwnerBoxSerializer,
    OwnerBoxCreateSerializer, MoneyLineInputSerializer, LoanCreateSerializer, AmanaCreateSerializer,
    PartEntryCreateSerializer, PartEntrySerializer, LoanSerializer, AmanaSerializer, PAIR_KINDS, OWNER_LINE_RELATED,
    owner_move_line, owner_counterparts, lines_of,
)
from .serializers import CountSerializer, TransferSerializer, ReverseSerializer, money
from .views import check_can_close, statement_answer, statement_dates

K = MoneyMove.Kind
# Lines the owner may reverse from here: they have no business source. A sale / refund / expense / other income /
# a PartEntry line is undone where it was made (that writes the reversal itself) → `reverse_at_source`.
OWNER_MANUAL_KINDS = set(PAIR_KINDS) | {K.OPENING, K.COUNT_ADJUST, K.LOAN_IN, K.LOAN_REPAY, K.LOAN_OUT, K.LOAN_BACK,
                                        K.AMANA_IN, K.AMANA_OUT, K.PARTNER_IN, K.PARTNER_OUT}
# Parts are listed personal first, then the shops, then the other businesses.
PART_ORDER = Case(When(kind=OwnerPart.Kind.PERSONAL, then=Value(0)), When(kind=OwnerPart.Kind.SHOP, then=Value(1)),
                  default=Value(2), output_field=IntegerField())


def _log_shop(request, box, action, details):
    """A line in the shop's activity log — only when `box` is a SHOP box (see the module docstring)."""
    if not box.store_id:
        return
    log_activity(request=request, action=action, op_type=ActivityLog.OperationType.OTHER, store=box.store,
                 details={'area': 'treasury', 'owner_level': True, 'box_id': str(box.id), **details})


def _with_balance(qs):
    return qs.annotate(balance_sum=Coalesce(Sum('moves__amount'), Value(ZERO)))


def _date_filters(qs, params, field='date'):
    for key, lookup in (('date_from', f'{field}__gte'), ('date_to', f'{field}__lte')):
        raw = (params.get(key) or '').strip()
        if raw:
            try:
                day = parse_date(raw)
            except ValueError:
                day = None
            if day is None:
                raise ValidationError({key: 'Use a date like 2026-01-31.'})
            qs = qs.filter(**{lookup: day})
    return qs


class OwnerViewSetMixin(OwnerMixin):
    # Every owner-level action needs the OWNER role (RoleScopedPermission's fail-closed default, said out loud).
    default_min_role = 'OWNER'

    def owner_parts_qs(self):
        return ledger.owner_parts(self.owner)


# ── overview ──────────────────────────────────────────────────────────────────────────────────────────────────
class OverviewViewSet(OwnerViewSetMixin, viewsets.ViewSet):
    """GET overview/ — every part the owner sees, its open boxes + total, and the owner's headline figures."""

    def list(self, request):
        parts = list(self.owner_parts_qs().select_related('store').annotate(o=PART_ORDER).order_by('o', 'name'))
        boxes = (_with_balance(ledger.owner_boxes(self.owner).filter(is_active=True).select_related('branch', 'part'))
                 .annotate(drawer_first=Case(When(kind=MoneyBox.Kind.DRAWER, then=Value(0)), default=Value(1),
                                             output_field=IntegerField()))
                 .order_by('drawer_first', 'name'))
        by_part = {}
        for b in boxes:
            by_part.setdefault(b.part_id, []).append(b)
        out, personal_total, business_total = [], ZERO, ZERO
        for p in parts:
            pboxes = by_part.get(p.id, [])
            total = sum((b.balance_sum for b in pboxes), ZERO)
            if p.kind == OwnerPart.Kind.PERSONAL:
                personal_total += total
            else:
                business_total += total
            out.append({**OwnerPartSerializer(p).data, 'boxes': OwnerBoxSerializer(pboxes, many=True).data,
                        'total': money(total)})
        loans = (MoneyMove.all_objects.filter(loan__owner=self.owner, loan__is_deleted=False, loan__part__in=parts)
                 .values('loan__direction').annotate(s=Sum('amount')))
        sums = {r['loan__direction']: r['s'] or ZERO for r in loans}
        amanat = (MoneyMove.all_objects.filter(amana__owner=self.owner, amana__is_deleted=False, amana__part__in=parts)
                  .aggregate(s=Sum('amount'))['s'] or ZERO)
        return Response({
            'parts': out,
            'personal_total': money(personal_total),
            'business_total': money(business_total),     # shops + businesses
            'amanat_held': money(amanat),
            'loans_owed_by_me': money(sums.get(Loan.Direction.BORROWED, ZERO)),
            'loans_owed_to_me': money(-sums.get(Loan.Direction.LENT, ZERO)),
        })


# ── parts ─────────────────────────────────────────────────────────────────────────────────────────────────────
class OwnerPartViewSet(OwnerViewSetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Parts: list / create a BUSINESS / rename / close a BUSINESS (only once every box is closed). A SHOP part belongs
    to its shop (never renamed or closed here); the PERSONAL part can be renamed, never closed."""
    serializer_class = OwnerPartSerializer
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        return self.owner_parts_qs().select_related('store').annotate(o=PART_ORDER).order_by('o', 'name')

    def create(self, request):
        ser = OwnerPartCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if ser.validated_data['kind'] != OwnerPart.Kind.BUSINESS:
            raise TreasuryError('part_business_only', 'Only a business can be added here.')
        part = OwnerPart.all_objects.create(owner=self.owner, kind=OwnerPart.Kind.BUSINESS,
                                            name=ser.validated_data['name'])
        return Response(OwnerPartSerializer(part).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        ser = OwnerPartPatchSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        with transaction.atomic():
            part = get_object_or_404(self.owner_parts_qs().select_for_update(), pk=pk)
            if 'name' in d:
                name = d['name'].strip()
                if part.kind == OwnerPart.Kind.SHOP:
                    raise TreasuryError('part_fixed', "A shop's part carries the shop's name.")
                if not name and part.kind == OwnerPart.Kind.BUSINESS:
                    raise ValidationError({'name': 'Give the business a name.'})
                part.name = name
                part.save(update_fields=['name', 'updated_at'])
            if d.get('is_active') is False:
                if part.kind != OwnerPart.Kind.BUSINESS:
                    raise TreasuryError('part_fixed', 'Only a business can be closed.')
                # A closed part disappears with its boxes: nothing may still hold money or a live debt.
                if (MoneyBox.all_objects.filter(part=part, is_active=True).exists()
                        or Loan.objects.filter(part=part, is_closed=False).exists()
                        or Amana.objects.filter(part=part, is_closed=False).exists()):
                    raise TreasuryError('part_not_empty', 'Close every box (and settle its loans and amanat) first.')
                part.delete()   # soft
        part.refresh_from_db()
        return Response(OwnerPartSerializer(part).data)


# ── boxes ─────────────────────────────────────────────────────────────────────────────────────────────────────
class OwnerBoxViewSet(OwnerViewSetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Boxes across every part the owner sees: list / create / rename-note-close + statement + count."""
    serializer_class = OwnerBoxSerializer
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        qs = ledger.owner_boxes(self.owner).select_related('branch', 'part')
        qs = qs.annotate(drawer_first=Case(When(kind=MoneyBox.Kind.DRAWER, then=Value(0)), default=Value(1),
                                            output_field=IntegerField()))
        qs = _with_balance(qs).order_by('-is_active', 'part__kind', 'drawer_first', 'name')
        p = self.request.query_params
        if p.get('active') in ('1', 'true'):
            qs = qs.filter(is_active=True)
        if p.get('part'):
            qs = qs.filter(part_id=self.owner_part(p['part']).pk)
        return qs

    def create(self, request):
        ser = OwnerBoxCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        with transaction.atomic():
            part = get_object_or_404(self.owner_parts_qs().select_for_update(), pk=d['part'])
            branch = None
            if d.get('branch'):
                if part.kind != OwnerPart.Kind.SHOP:
                    raise ValidationError({'branch': 'Only a shop box can belong to a branch.'})
                branch = get_object_or_404(Branch.all_objects.filter(store_id=part.store_id, is_deleted=False),
                                           pk=d['branch'])
            if d['kind'] == MoneyBox.Kind.DRAWER:
                if part.kind != OwnerPart.Kind.SHOP:
                    raise TreasuryError('drawer_needs_shop', 'A till drawer belongs to a shop.')
                if branch is None:
                    raise TreasuryError('drawer_needs_branch', 'A till drawer belongs to a branch — choose one.')
            box = ledger.open_box(part=part, name=d['name'], kind=d['kind'], user=request.user, opening=d['opening'],
                                  branch=branch, note=d['note'], allow_negative=d['allow_negative'])
        _log_shop(request, box, f'Owner level: created money box {box.name}',
                  {'kind': box.kind, 'opening': money(d['opening'])})
        return Response(self.get_serializer(self.get_queryset().get(pk=box.pk)).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        with transaction.atomic():
            box = self.get_object()
            ser = self.get_serializer(box, data=request.data, partial=True)
            ser.is_valid(raise_exception=True)
            if 'name' in ser.validated_data:
                ser.validated_data['name'] = ser.validated_data['name'].strip()
                if not ser.validated_data['name'] and box.system_role != MoneyBox.Role.UNSORTED:
                    raise ValidationError({'name': 'Give the box a name.'})
            closing = box.is_active and ser.validated_data.get('is_active') is False
            if closing:
                check_can_close(box)
            ser.save()
        if closing:
            _log_shop(request, box, f'Owner level: closed money box {box.name}', {})
        return Response(self.get_serializer(self.get_queryset().get(pk=box.pk)).data)

    @action(detail=True, methods=['get'])
    def statement(self, request, pk=None):
        """The Wave 1 statement (same maths, same shape) for any box of the owner, with the owner's labels."""
        box = self.get_object()
        df, dt, error = statement_dates(request.query_params)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        lines_qs = MoneyMove.all_objects.filter(box=box).select_related(*OWNER_LINE_RELATED)
        return Response({'box': self.get_serializer(box).data,
                         **statement_answer(lines_qs, df, dt, box.id, owner_move_line, owner_counterparts)})

    @action(detail=True, methods=['post'], url_path='count')
    def count_box(self, request, pk=None):
        box = self.get_object()
        ser = CountSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            move = ledger.count(box, ser.validated_data['counted'], user=request.user,
                                reason=ser.validated_data['reason'])
            bal = ledger.balance(box)
        diff = move.amount if move else ZERO
        if move:
            _log_shop(request, box, f'Owner level: counted money box {box.name} (difference {money(diff)})',
                      {'move_id': str(move.id), 'difference': money(diff), 'reason': move.reason})
        return Response({'difference': money(diff), 'move': owner_move_line(move) if move else None,
                         'balance': money(bal)})


# ── moving money ──────────────────────────────────────────────────────────────────────────────────────────────
class OwnerMoveViewSet(OwnerViewSetMixin, viewsets.ViewSet):
    """POST moves/ {from_box, to_box, amount, note?} — any two boxes of the owner. Same part = a transfer; leaving a
    business = an owner draw; arriving in one = a put-in; business → business = both. Never income, never expense."""

    def create(self, request):
        ser = TransferSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        src, dst = self.owner_box(d['from_box']), self.owner_box(d['to_box'])
        with transaction.atomic():
            out, inn, label = ledger.move_between(src, dst, d['amount'], user=request.user, note=d['note'])
        for box, other in ((src, dst), (dst, src)):
            # Staff see "Owner draw" / "Owner put in"; the shop's log names only the shop box, never a personal one.
            _log_shop(request, box, f'Owner level: money moved ({label.lower()}) — {box.name}',
                      {'transfer_id': str(out.transfer_id), 'amount': money(d['amount'])})
        out.box, inn.box = src, dst
        return Response({'kind': label,
                         'out': owner_move_line(out, counterpart_names={out.transfer_id: dst.name}),
                         'in': owner_move_line(inn, counterpart_names={inn.transfer_id: src.name})},
                        status=status.HTTP_201_CREATED)


# ── lines ─────────────────────────────────────────────────────────────────────────────────────────────────────
class OwnerLineViewSet(OwnerViewSetMixin, viewsets.GenericViewSet):
    """POST lines/{id}/reverse/ {reason} — lines with no business source; a two-line move reverses BOTH legs."""

    def get_queryset(self):
        return MoneyMove.all_objects.filter(box__in=ledger.owner_boxes(self.owner))

    @action(detail=True, methods=['post'], url_path='reverse')
    def reverse_line(self, request, pk=None):
        move = self.get_object()
        ser = ReverseSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        reason = ser.validated_data['reason']
        if move.kind == K.REVERSAL:
            raise TreasuryError('cannot_reverse_reversal', 'A reversal line cannot be reversed.')
        if move.kind not in OWNER_MANUAL_KINDS or move.part_entry_id:
            raise TreasuryError('reverse_at_source', 'This line comes from a sale, refund, payment, expense or income '
                                                     '— undo it where it was made.', kind=move.kind)
        with transaction.atomic():
            if move.transfer_id and move.kind in PAIR_KINDS:
                legs = list(MoneyMove.all_objects.filter(transfer_id=move.transfer_id, kind__in=PAIR_KINDS,
                                                         box__owner=self.owner))
                # Take the money back out of the receiving box first (the floor check), then return it to the sender.
                legs.sort(key=lambda m: 0 if m.amount > 0 else 1)
            else:
                legs = [move]
            reversals = [ledger.reverse(m, user=request.user, reason=reason) for m in legs]
            # A reversal must never leave a loan owing less than nothing, or an amana holding less than nothing
            # (e.g. reversing the money lent after part of it came back) — refuse it, nothing is written.
            if move.loan_id and ledger.loan_owed(Loan.objects.get(pk=move.loan_id)) < 0:
                raise TreasuryError('loan_overpaid', 'Reversing this line would leave the loan below zero — reverse '
                                                     'the later payments first.')
            if move.amana_id and ledger.amana_held(Amana.objects.get(pk=move.amana_id)) < 0:
                raise TreasuryError('amana_overdrawn', 'Reversing this line would leave less than nothing held — '
                                                       'reverse the later returns first.')
        rev_ids = [r.id for r in reversals]
        rows = {r.id: r for r in MoneyMove.all_objects.filter(id__in=rev_ids).select_related(
            'created_by', 'reverses', *OWNER_LINE_RELATED)}
        out = []
        for r in reversals:
            r = rows[r.id]
            other = next((o.box.name for o in rows.values() if o.box_id != r.box_id), '')
            out.append(owner_move_line(r, counterpart_names={r.transfer_id: other} if r.transfer_id else None))
            _log_shop(request, r.box, f'Owner level: reversed a money line ({move.kind}) — {r.box.name}',
                      {'move_id': str(move.id), 'reversal_id': str(r.id), 'amount': money(r.amount),
                       'reason': reason})
        return Response({'reversals': out}, status=status.HTTP_201_CREATED)


# ── income / spending of a business or personal money ─────────────────────────────────────────────────────────
class PartEntryViewSet(OwnerViewSetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """GET entries/?part=&direction=&date_from=&date_to= · POST entries/ · DELETE entries/{id}/ (soft + reversal)."""
    serializer_class = PartEntrySerializer
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        qs = (PartEntry.objects.filter(owner=self.owner, part__in=self.owner_parts_qs())
              .select_related('part', 'box'))
        p = self.request.query_params
        if p.get('part'):
            qs = qs.filter(part_id=self.owner_part(p['part']).pk)
        if p.get('direction') in PartEntry.Direction.values:
            qs = qs.filter(direction=p['direction'])
        return _date_filters(qs, p)

    def create(self, request):
        ser = PartEntryCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        part, box = self.owner_part(d['part']), self.owner_box(d['box'])
        with transaction.atomic():
            entry = ledger.entry_create(part, d['direction'], d['amount'], box, user=request.user,
                                        category=d['category'], date=d['date'], description=d['description'],
                                        owner_only=d['owner_only'])
        return Response(PartEntrySerializer(self.get_queryset().get(pk=entry.pk)).data,
                        status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None):
        entry = self.get_object()
        reason = (request.data.get('reason') if hasattr(request.data, 'get') else '') or 'Entry deleted'
        with transaction.atomic():
            ledger.entry_delete(entry, user=request.user, reason=reason)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── loans ─────────────────────────────────────────────────────────────────────────────────────────────────────
class LoanViewSet(OwnerViewSetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Money borrowed / lent. GET loans/?part=&direction=&open=1 · POST loans/ · GET loans/{id}/ (+ lines) ·
    POST loans/{id}/pay/ · POST loans/{id}/more/. Owed hits 0 → closed (the ledger keeps the mark true)."""
    serializer_class = LoanSerializer
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        qs = (Loan.objects.filter(owner=self.owner, part__in=self.owner_parts_qs()).select_related('part')
              .annotate(line_sum=Coalesce(Sum('moves__amount'), Value(ZERO))))
        p = self.request.query_params
        if p.get('part'):
            qs = qs.filter(part_id=self.owner_part(p['part']).pk)
        if p.get('direction') in Loan.Direction.values:
            qs = qs.filter(direction=p['direction'])
        if p.get('open') in ('1', 'true'):
            qs = qs.filter(is_closed=False)
        return qs

    def _detail(self, loan_id):
        loan = self.get_queryset().get(pk=loan_id)
        return {**LoanSerializer(loan).data, 'lines': lines_of(MoneyMove.all_objects.filter(loan=loan))}

    def retrieve(self, request, pk=None):
        return Response(self._detail(self.get_object().pk))

    def create(self, request):
        ser = LoanCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        part, box = self.owner_part(d['part']), self.owner_box(d['box'])
        with transaction.atomic():
            loan = ledger.loan_open(part, d['direction'], d['counterparty'], d['amount'], box, user=request.user,
                                    note=d['note'])
        _log_shop(request, box, f'Owner level: loan line — {box.name}', {'amount': money(d['amount'])})
        return Response(self._detail(loan.pk), status=status.HTTP_201_CREATED)

    def _line(self, request, door):
        loan = self.get_object()
        ser = MoneyLineInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        box = self.owner_box(d['box'])
        with transaction.atomic():
            door(loan, d['amount'], box, user=request.user, note=d['note'])
        _log_shop(request, box, f'Owner level: loan line — {box.name}', {'amount': money(d['amount'])})
        return Response(self._detail(loan.pk), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def pay(self, request, pk=None):
        return self._line(request, ledger.loan_pay)

    @action(detail=True, methods=['post'])
    def more(self, request, pk=None):
        return self._line(request, ledger.loan_more)


# ── amanat (money held for others) ────────────────────────────────────────────────────────────────────────────
class AmanaViewSet(OwnerViewSetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """GET amanat/?part=&open=1 · POST amanat/ · GET amanat/{id}/ (+ lines) · POST amanat/{id}/return/ ·
    POST amanat/{id}/more/. Never returns more than is held (`amana_overdrawn`)."""
    serializer_class = AmanaSerializer
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        qs = (Amana.objects.filter(owner=self.owner, part__in=self.owner_parts_qs()).select_related('part')
              .annotate(line_sum=Coalesce(Sum('moves__amount'), Value(ZERO))))
        p = self.request.query_params
        if p.get('part'):
            qs = qs.filter(part_id=self.owner_part(p['part']).pk)
        if p.get('open') in ('1', 'true'):
            qs = qs.filter(is_closed=False)
        return qs

    def _detail(self, amana_id):
        amana = self.get_queryset().get(pk=amana_id)
        return {**AmanaSerializer(amana).data, 'lines': lines_of(MoneyMove.all_objects.filter(amana=amana))}

    def retrieve(self, request, pk=None):
        return Response(self._detail(self.get_object().pk))

    def create(self, request):
        ser = AmanaCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        part, box = self.owner_part(d['part']), self.owner_box(d['box'])
        with transaction.atomic():
            amana = ledger.amana_receive(part, d['held_for'], d['amount'], box, user=request.user, note=d['note'])
        _log_shop(request, box, f'Owner level: amana line — {box.name}', {'amount': money(d['amount'])})
        return Response(self._detail(amana.pk), status=status.HTTP_201_CREATED)

    def _line(self, request, door):
        amana = self.get_object()
        ser = MoneyLineInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        box = self.owner_box(d['box'])
        with transaction.atomic():
            door(amana, d['amount'], box, user=request.user, note=d['note'])
        _log_shop(request, box, f'Owner level: amana line — {box.name}', {'amount': money(d['amount'])})
        return Response(self._detail(amana.pk), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='return')
    def return_money(self, request, pk=None):
        return self._line(request, ledger.amana_return)

    @action(detail=True, methods=['post'])
    def more(self, request, pk=None):
        return self._line(request, ledger.amana_more)
