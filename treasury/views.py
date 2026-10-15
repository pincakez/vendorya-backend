"""Treasury API (TREASURY.md § 11 part A) — `/api/treasury/`.

Every endpoint: shop staff only (sudo refused like everywhere — RoleScopedPermission), scoped to `request.user.store`
(boxes by `store`, lines by `store` too — copied from the box), and 403 `{"code": "treasury_off"}` while the shop's
Treasury switch is off. Every money line is written through `treasury.ledger` (the ONE door) inside a transaction;
a refused move (`TreasuryError`) answers 400 `{"code", "detail", **params}` — the UI translates `code`.
"""
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Case, Exists, IntegerField, OuterRef, Sum, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.activity import log_activity
from core.models import ActivityLog, Branch
from users.permissions import RoleScopedPermission

from . import ledger
from .ledger import TreasuryError, ZERO
from .models import MoneyBox, MoneyMove, IncomeCategory, OtherIncome
from .serializers import (
    MoneyBoxSerializer, MoneyBoxCreateSerializer, CountSerializer, TransferSerializer, ReverseSerializer,
    IncomeCategorySerializer, OtherIncomeSerializer, move_line, transfer_counterparts, money, owner_level_type,
)

# Lines a person may reverse from the Treasury: they have no business source. Everything else (a sale, a refund,
# an expense, other income…) is undone where it was made, which writes the reversal itself.
MANUAL_KINDS = {MoneyMove.Kind.TRANSFER_IN, MoneyMove.Kind.TRANSFER_OUT, MoneyMove.Kind.OPENING,
                MoneyMove.Kind.COUNT_ADJUST}


def treasury_error_response(exc):
    """THE mapping of a refused money move to HTTP: 400 `{"code", "detail", **params}`. Other apps writing ledger
    lines (finance / services flows) use it too, so the UI meets one shape everywhere."""
    return Response({'code': exc.code, 'detail': str(exc), **exc.params}, status=status.HTTP_400_BAD_REQUEST)


def _with_balance(qs):
    """Annotate each box with the sum of its lines (one query for a whole list — no N+1)."""
    return qs.annotate(balance_sum=Coalesce(Sum('moves__amount'), Value(ZERO)))


def _log(request, action, details):
    log_activity(request=request, action=action, op_type=ActivityLog.OperationType.OTHER,
                 details={'area': 'treasury', **details})


def check_can_close(box):
    """Closing ≠ deleting — but only an empty box, never the "not sorted yet" box, never a till drawer the
    shift flows rely on (the branch's default drawer — cash with no shift lands there — or one with an open shift).
    Shared by the shop's Treasury and the owner level (a box of any part; the checks follow the box, not the login)."""
    from finance.models import PaymentMethod, WorkShift
    if box.system_role == MoneyBox.Role.UNSORTED:
        raise TreasuryError('box_system', 'The "not sorted yet" box cannot be closed.')
    if box.is_default_drawer:
        raise TreasuryError('box_default_drawer', "A branch's main till drawer cannot be closed.")
    if WorkShift.all_objects.filter(drawer=box, status=WorkShift.Status.OPEN).exists():
        raise TreasuryError('drawer_busy', 'A shift is open on this drawer — close the shift first.')
    # A payment method still sends its money here → the next card/e-wallet sale would be refused (box_closed).
    users = list(PaymentMethod.all_objects.filter(lands_in=box, is_deleted=False).values_list('name', flat=True))
    if users:
        raise TreasuryError('box_in_use', 'A payment method still sends its money to this box — choose another box '
                            'for it first.', methods=', '.join(users))
    ledger._lock(box)
    bal = ledger.balance(box)
    if bal != 0:
        raise TreasuryError('box_not_empty', 'Only an empty box can be closed — move its money out first.',
                            balance=money(bal))


def statement_dates(params):
    """`date_from` / `date_to` (YYYY-MM-DD, both optional) → (date_from, date_to, error). `error` is the 400 body."""
    dates = {}
    for key in ('date_from', 'date_to'):
        raw = (params.get(key) or '').strip()
        try:
            dates[key] = parse_date(raw) if raw else None
        except ValueError:   # well-formed but impossible, e.g. 2026-02-31
            dates[key] = None
        if raw and dates[key] is None:
            return None, None, {key: 'Use a date like 2026-01-31.'}
    df, dt = dates['date_from'], dates['date_to']
    if df and dt and df > dt:
        return None, None, {'date_to': 'The end date is before the start date.'}
    return df, dt, None


# What a statement line may point at (one query for the whole statement).
STATEMENT_RELATED = ('created_by', 'payment__invoice', 'customer_receipt__customer', 'refund',
                     'supplier_payment__supplier', 'expense__category', 'other_income__category', 'shift__user',
                     'reverses')


def shop_move_line(m, running_balance=None, counterpart_names=None):
    """A statement line as SHOP STAFF see it: an owner-level line (draw, put-in, loan, amana, partner — or its
    reversal) keeps only its generic label; the owner typed its note / reason for themself (§ 12.4 privacy).
    Never used for the owner's own statements (they share `statement_answer` with their own line maker)."""
    line = move_line(m, running_balance, counterpart_names)
    if owner_level_type(m):
        line['note'] = line['reason'] = ''
    return line


def statement_answer(lines_qs, df, dt, box_id, make_line, counterparts):
    """THE statement maths (shop Treasury + owner level): the lines of `lines_qs` oldest first, each with its running
    balance; `opening_balance` = every line before date_from (local day). No dates = all. `make_line(m, running, names)`
    shapes one line; `counterparts(moves, box_id)` names the other box of each transfer-like pair."""
    tz = timezone.get_current_timezone()
    opening = ZERO
    if df:
        start = timezone.make_aware(datetime.combine(df, time.min), tz)
        opening = lines_qs.filter(at__lt=start).aggregate(s=Sum('amount'))['s'] or ZERO
        lines_qs = lines_qs.filter(at__gte=start)
    if dt:
        lines_qs = lines_qs.filter(at__lt=timezone.make_aware(datetime.combine(dt + timedelta(days=1), time.min), tz))
    moves = list(
        lines_qs.select_related(*STATEMENT_RELATED)
        .annotate(is_reversed=Exists(MoneyMove.all_objects.filter(reverses=OuterRef('pk'))))
        .order_by('at', 'created_at', 'id'))
    names = counterparts(moves, box_id)

    running = opening
    total_in = total_out = ZERO
    lines = []
    for m in moves:
        running += m.amount
        if m.amount > 0:
            total_in += m.amount
        else:
            total_out -= m.amount
        lines.append(make_line(m, running, names))
    return {
        'date_from': df, 'date_to': dt,
        'opening_balance': money(opening),
        'lines': lines,
        'total_in': money(total_in),      # money in (positive)
        'total_out': money(total_out),    # money out (positive) — closing = opening + in − out
        'closing_balance': money(running),
    }


class TreasuryMixin:
    """Shared gate for every Treasury endpoint: role (role_map) → a shop → the switch on. Maps TreasuryError → 400."""
    permission_classes = [IsAuthenticated, RoleScopedPermission]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)   # authentication + RoleScopedPermission (sudo refused)
        store = getattr(request.user, 'store', None)
        if store is None:
            raise PermissionDenied({'code': 'no_store', 'detail': 'User has no store assigned.'})
        if not ledger.is_on(store):
            raise PermissionDenied({'code': 'treasury_off', 'detail': 'The Treasury is turned off for this shop.'})

    def handle_exception(self, exc):
        if isinstance(exc, TreasuryError):
            return treasury_error_response(exc)
        return super().handle_exception(exc)

    @property
    def store(self):
        return self.request.user.store

    def shop_box(self, box_id):
        """A box of the caller's shop, else 404 (another shop's box is never revealed)."""
        return get_object_or_404(MoneyBox.objects.filter(store=self.store), pk=box_id)


# ── boxes ───────────────────────────────────────────────────────────────────────────────────────────────────────
class MoneyBoxViewSet(TreasuryMixin, viewsets.ModelViewSet):
    """Money boxes: list / create / rename-note-close + the statement and a count."""
    serializer_class = MoneyBoxSerializer
    http_method_names = ['get', 'post', 'patch', 'head', 'options']
    role_map = {
        'list':           'MANAGER',
        'retrieve':       'MANAGER',
        'create':         'OWNER',
        'partial_update': 'OWNER',
        'statement':      'MANAGER',
        'count_box':      'MANAGER',
    }

    def get_queryset(self):
        qs = MoneyBox.objects.filter(store=self.store).select_related('branch')
        # Drawers first, then the other boxes by name; closed boxes last.
        qs = qs.annotate(drawer_first=Case(When(kind=MoneyBox.Kind.DRAWER, then=Value(0)), default=Value(1),
                                            output_field=IntegerField()))
        qs = _with_balance(qs).order_by('-is_active', 'drawer_first', 'name')
        active = self.request.query_params.get('active')
        if active in ('1', 'true'):
            qs = qs.filter(is_active=True)
        return qs

    def create(self, request, *args, **kwargs):
        ser = MoneyBoxCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        branch = None
        if data.get('branch'):
            branch = get_object_or_404(Branch.objects.filter(store=self.store, is_deleted=False), pk=data['branch'])
        if data['kind'] == MoneyBox.Kind.DRAWER and branch is None:
            raise TreasuryError('drawer_needs_branch', 'A till drawer belongs to a branch — choose one.')
        with transaction.atomic():
            box = ledger.open_box(part=ledger.shop_part(self.store), name=data['name'], kind=data['kind'],
                                  user=request.user, opening=data['opening'], branch=branch, note=data['note'],
                                  allow_negative=data['allow_negative'])
        _log(request, f'Created money box: {box.name}',
             {'box_id': str(box.id), 'kind': box.kind, 'opening': money(data['opening'])})
        return Response(self.get_serializer(self.get_queryset().get(pk=box.pk)).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        with transaction.atomic():
            box = self.get_object()
            ser = self.get_serializer(box, data=request.data, partial=True)
            ser.is_valid(raise_exception=True)
            if 'name' in ser.validated_data:
                ser.validated_data['name'] = ser.validated_data['name'].strip()
                if not ser.validated_data['name'] and box.system_role != MoneyBox.Role.UNSORTED:
                    raise ValidationError({'name': 'Give the box a name.'})
            if box.is_active and ser.validated_data.get('is_active') is False:
                check_can_close(box)
            ser.save()
        return Response(self.get_serializer(self.get_queryset().get(pk=box.pk)).data)

    @action(detail=True, methods=['get'])
    def statement(self, request, pk=None):
        """GET boxes/{id}/statement/?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD — the box's lines oldest first, each with
        its running balance. `opening_balance` = every line before date_from (shop's local day). No dates = all."""
        box = self.get_object()
        df, dt, error = statement_dates(request.query_params)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        lines_qs = MoneyMove.objects.filter(store=self.store, box=box)
        return Response({'box': self.get_serializer(box).data,
                         **statement_answer(lines_qs, df, dt, box.id, shop_move_line, transfer_counterparts)})

    @action(detail=True, methods=['post'], url_path='count')
    def count_box(self, request, pk=None):
        """POST boxes/{id}/count/ {counted, reason?} — reality vs book; a difference needs a reason and writes one
        COUNT_ADJUST line. Answer: {difference, move (null when equal), balance}."""
        box = self.get_object()
        ser = CountSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            move = ledger.count(box, ser.validated_data['counted'], user=request.user,
                                reason=ser.validated_data['reason'])
            bal = ledger.balance(box)
        diff = move.amount if move else ZERO
        if move:
            _log(request, f'Counted money box: {box.name} (difference {money(diff)})',
                 {'box_id': str(box.id), 'move_id': str(move.id), 'counted': money(ser.validated_data['counted']),
                  'difference': money(diff), 'reason': move.reason})
        return Response({'difference': money(diff), 'move': move_line(move) if move else None,
                         'balance': money(bal)})


# ── transfers ───────────────────────────────────────────────────────────────────────────────────────────────────
class TransferViewSet(TreasuryMixin, viewsets.ViewSet):
    """POST transfers/ {from_box, to_box, amount, note?} — two lines written together (or none). Never income or expense."""
    role_map = {'create': 'ADMIN'}

    def create(self, request):
        ser = TransferSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        src, dst = self.shop_box(d['from_box']), self.shop_box(d['to_box'])
        with transaction.atomic():
            out, inn = ledger.transfer(src, dst, d['amount'], user=request.user, note=d['note'])
        _log(request, f'Transfer: {money(d["amount"])} from {src.name} to {dst.name}',
             {'transfer_id': str(out.transfer_id), 'from_box': str(src.id), 'to_box': str(dst.id),
              'amount': money(d['amount'])})
        return Response({'out': move_line(out, counterpart_names={out.transfer_id: dst.name}),
                         'in': move_line(inn, counterpart_names={inn.transfer_id: src.name})},
                        status=status.HTTP_201_CREATED)


# ── lines ───────────────────────────────────────────────────────────────────────────────────────────────────────
class MoneyMoveViewSet(TreasuryMixin, viewsets.GenericViewSet):
    """POST moves/{id}/reverse/ {reason} — only lines with no business source; a transfer reverses BOTH legs."""
    role_map = {'reverse_move': 'ADMIN'}

    def get_queryset(self):
        return MoneyMove.objects.filter(store=self.store)

    @action(detail=True, methods=['post'], url_path='reverse')
    def reverse_move(self, request, pk=None):
        move = self.get_object()
        ser = ReverseSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        reason = ser.validated_data['reason']
        if move.kind == MoneyMove.Kind.REVERSAL:
            raise TreasuryError('cannot_reverse_reversal', 'A reversal line cannot be reversed.')
        if move.kind not in MANUAL_KINDS:
            raise TreasuryError('reverse_at_source', 'This line comes from a sale, refund, payment, expense or income '
                                                     '— undo it where it was made.', kind=move.kind)
        with transaction.atomic():
            if move.transfer_id:
                legs = list(MoneyMove.all_objects.filter(
                    transfer_id=move.transfer_id, store=self.store,
                    kind__in=[MoneyMove.Kind.TRANSFER_OUT, MoneyMove.Kind.TRANSFER_IN]))
                # Take the money back out of the receiving box first (the floor check), then return it to the sender.
                legs.sort(key=lambda m: 0 if m.kind == MoneyMove.Kind.TRANSFER_IN else 1)
            else:
                legs = [move]
            reversals = [ledger.reverse(m, user=request.user, reason=reason) for m in legs]
        _log(request, f'Reversed money line ({move.kind})',
             {'move_id': str(move.id), 'kind': move.kind, 'transfer_id': str(move.transfer_id or '') or None,
              'reversals': [str(r.id) for r in reversals], 'reason': reason})
        out = []
        for r in reversals:
            # A transfer reversal's ref label = the OTHER box of the transfer, like the original legs.
            other = next((o.box.name for o in reversals if o.box_id != r.box_id), '')
            out.append(move_line(r, counterpart_names={r.transfer_id: other} if r.transfer_id else None))
        return Response({'reversals': out}, status=status.HTTP_201_CREATED)


# ── other income ────────────────────────────────────────────────────────────────────────────────────────────────
class IncomeCategoryViewSet(TreasuryMixin, viewsets.ModelViewSet):
    serializer_class = IncomeCategorySerializer
    http_method_names = ['get', 'post', 'patch', 'put', 'delete', 'head', 'options']
    role_map = {
        'list':           'MANAGER',
        'retrieve':       'MANAGER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
    }

    def get_queryset(self):
        return IncomeCategory.objects.filter(store=self.store).order_by('name')

    def perform_create(self, serializer):
        serializer.save(store=self.store)

    def perform_destroy(self, instance):
        instance.delete()   # soft (SoftDeleteModel); old income keeps its category


class OtherIncomeViewSet(TreasuryMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                         mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Money in that isn't a sale. Create → an OTHER_INCOME_IN line in its box; delete → soft + the REVERSAL line,
    in one transaction (refused when the money already left a cash box — `not_enough_money`)."""
    serializer_class = OtherIncomeSerializer
    role_map = {
        'list':     'MANAGER',
        'retrieve': 'MANAGER',
        'create':   'MANAGER',
        'destroy':  'ADMIN',
    }

    def get_queryset(self):
        qs = (OtherIncome.objects.filter(store=self.store)
              .select_related('box', 'category', 'branch', 'created_by')
              .order_by('-date', '-created_at'))
        p = self.request.query_params
        if p.get('box'):
            qs = qs.filter(box_id=p['box'])
        for key, lookup in (('date_from', 'date__gte'), ('date_to', 'date__lte')):
            raw = (p.get(key) or '').strip()
            if raw:
                try:
                    day = parse_date(raw)
                except ValueError:
                    day = None
                if day is None:
                    raise ValidationError({key: 'Use a date like 2026-01-31.'})
                qs = qs.filter(**{lookup: day})
        return qs

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        box = self.shop_box(d['box_id'])
        category = None
        if d.get('category_id'):
            category = get_object_or_404(IncomeCategory.objects.filter(store=self.store), pk=d['category_id'])
        branch = None
        if d.get('branch_id'):
            branch = get_object_or_404(Branch.objects.filter(store=self.store, is_deleted=False), pk=d['branch_id'])
        with transaction.atomic():
            income = OtherIncome.objects.create(
                store=self.store, box=box, category=category, branch=branch, amount=d['amount'],
                date=d.get('date') or timezone.localdate(), description=d.get('description', ''),
                created_by=request.user)
            ledger.post(box, income.amount, MoneyMove.Kind.OTHER_INCOME_IN, user=request.user,
                        note=income.description, other_income=income)
        _log(request, f'Other income: {money(income.amount)} into {box.name}',
             {'other_income_id': str(income.id), 'box_id': str(box.id), 'amount': money(income.amount),
              'category': category.name if category else None})
        return Response(self.get_serializer(income).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        reason = (request.data.get('reason') if hasattr(request.data, 'get') else '') or 'Other income deleted'
        with transaction.atomic():
            income = get_object_or_404(OtherIncome.objects.select_for_update().filter(store=self.store),
                                       pk=kwargs.get('pk'))
            for line in MoneyMove.all_objects.filter(other_income=income, kind=MoneyMove.Kind.OTHER_INCOME_IN,
                                                     reversed_by__isnull=True):
                ledger.reverse(line, user=request.user, reason=reason)
            income.delete()   # soft
        _log(request, f'Deleted other income: {money(income.amount)}',
             {'other_income_id': str(income.id), 'amount': money(income.amount), 'reason': reason})
        return Response(status=status.HTTP_204_NO_CONTENT)
