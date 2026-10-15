"""The ONE door to the Treasury Ledger (TREASURY.md). Every money-box line is written here, inside the caller's
transaction, with the box row locked. Nothing else creates a MoneyMove.

Public API (used by finance / services flows and the treasury API):
  is_on(store)                       — the shop's Treasury switch (StoreSettings.treasury_enabled)
  setup(store, user=None)            — Owner + SHOP part + one default drawer per branch + the "not sorted yet" box (idempotent)
  owner_for_store(store)             — the person (Owner) behind a shop; one person with two shops = one Owner
  shop_part(store)                   — the shop's SHOP part
  balance(box, at=None)              — sum of the box's lines (up to `at`)
  post(box, amount, kind, ...)       — one line; refuses to take a no-negative box below zero (except a count)
  reverse(move, user, reason)        — the opposite line, pointing at the original; once only
  transfer(src, dst, amount, ...)    — two lines (out + in) sharing a transfer_id; same owner only
  count(box, counted, user, reason)  — reality vs book → a COUNT_ADJUST line for the difference (reason required)
  open_box(..)                       — make a new box (+ an OPENING line when the opening amount ≠ 0)
  default_drawer(branch)             — the branch's default till drawer box
  unsorted_box(store)                — the shop's "not sorted yet" box
  box_for_method(method, shift=None, branch=None)
                                     — where a payment by `method` lands: cash → the shift's drawer (else the branch's
                                       default drawer); Ajel / store credit → None (no money moved); other → the method's
                                       `lands_in` box, else the "not sorted yet" box

Wave 2 — the owner level (TREASURY.md § 12), behind the second switch:
  owner_level_on(store) · owner_for_user(user) · setup_owner_level(store) · personal_part(owner) · owner_parts(owner) ·
  owner_boxes(owner) · move_between(src, dst, amount) (transfer / owner draw / put in — kinds from the two parts) ·
  loan_open / loan_more / loan_pay / loan_owed · amana_receive / amana_more / amana_return / amana_held / amanat_total ·
  partner_put_in / partner_pay / partner_account · check_shares · part_profit · settlement_preview / settle / cancel_settlement
"""
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import (Owner, OwnerPart, MoneyBox, MoneyMove, PartEntry, Loan, Amana, Partner, PartnerSettlement,
                     PartnerSettlementLine)

ZERO = Decimal('0')
CENT = Decimal('0.01')

# Kinds that may hold a negative balance by default (an overdraft); every other kind is cash-like.
NEGATIVE_OK_KINDS = {MoneyBox.Kind.BANK}
# What a line can point at (a reversal copies them, so a reversed line drops out of every per-source sum).
SOURCE_LINKS = ('payment', 'customer_receipt', 'refund', 'supplier_payment', 'expense', 'other_income', 'shift',
                'part_entry', 'loan', 'amana', 'partner')
# Parts that earn money (a P&L, partners). PERSONAL money is never profit.
BUSINESS_PARTS = {OwnerPart.Kind.SHOP, OwnerPart.Kind.BUSINESS}


class TreasuryError(Exception):
    """A refused money move — `code` is a stable key the API/UI translates."""
    def __init__(self, code, message, **params):
        super().__init__(message)
        self.code = code
        self.params = params


def _q(amount):
    return Decimal(str(amount)).quantize(CENT)


# ── switch + setup ──────────────────────────────────────────────────────────────────────────────────────────────
def is_on(store):
    if store is None:
        return False
    from core.models import StoreSettings
    return bool(StoreSettings.objects.filter(store=store).values_list('treasury_enabled', flat=True).first())


def owner_for_store(store):
    """The Owner behind `store`: the one already linked to the shop's part, else the Owner of the shop's OWNER login
    (so a person's second shop joins the SAME Owner), else a new one."""
    part = OwnerPart.all_objects.filter(store=store, is_deleted=False).select_related('owner').first()
    if part:
        return part.owner
    owner_user = getattr(store, 'owner', None)
    owner = Owner.objects.filter(users=owner_user).first() if owner_user else None
    if owner is None:
        name = (owner_user.get_full_name() or owner_user.username) if owner_user else ''
        owner = Owner.objects.create(display_name=name)
        if owner_user:
            owner.users.add(owner_user)
    return owner


def shop_part(store):
    part = OwnerPart.all_objects.filter(store=store, is_deleted=False).first()
    if part:
        return part
    return OwnerPart.all_objects.create(owner=owner_for_store(store), kind=OwnerPart.Kind.SHOP, store=store,
                                        name=store.name)


@transaction.atomic
def setup(store, user=None):
    """Everything a shop needs the moment it turns the Treasury on. Safe to call again (new branches get a drawer)."""
    from core.models import Branch
    part = shop_part(store)
    for branch in Branch.objects.filter(store=store, is_deleted=False):
        if not MoneyBox.all_objects.filter(branch=branch, is_default_drawer=True).exists():
            MoneyBox.all_objects.create(owner=part.owner, part=part, store=store, branch=branch, name=branch.name,
                                        kind=MoneyBox.Kind.DRAWER, is_default_drawer=True)
    unsorted_box(store, part=part)
    return part


def unsorted_box(store, part=None):
    box = MoneyBox.all_objects.filter(store=store, system_role=MoneyBox.Role.UNSORTED).first()
    if box:
        return box
    part = part or shop_part(store)
    return MoneyBox.all_objects.create(owner=part.owner, part=part, store=store, name='',
                                       kind=MoneyBox.Kind.WALLET, system_role=MoneyBox.Role.UNSORTED,
                                       allow_negative=True)


def default_drawer(branch):
    if branch is None:
        return None
    return MoneyBox.all_objects.filter(branch=branch, is_default_drawer=True, is_active=True).first()


def box_for_method(method, shift=None, branch=None):
    if method is None or method.is_agel or getattr(method, 'is_store_credit', False):
        return None
    if method.is_cash:
        drawer = getattr(shift, 'drawer', None) if shift is not None else None
        return drawer or default_drawer(branch if branch is not None else getattr(shift, 'branch', None))
    if getattr(method, 'lands_in_id', None):
        return method.lands_in
    return unsorted_box(method.store)


# ── reading ─────────────────────────────────────────────────────────────────────────────────────────────────────
def balance(box, at=None):
    qs = MoneyMove.all_objects.filter(box=box)
    if at is not None:
        qs = qs.filter(at__lte=at)
    return qs.aggregate(s=Sum('amount'))['s'] or ZERO


# ── writing ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _lock(box):
    return MoneyBox.all_objects.select_for_update().get(pk=box.pk)


@transaction.atomic
def post(box, amount, kind, *, user=None, at=None, note='', reason='', check_floor=True, **links):
    """Write ONE line. `amount` is signed (+ in / − out). A no-negative box can't go below zero unless it is a count
    (reality) or `check_floor=False` (a reversal of money that already left)."""
    amount = _q(amount)
    if amount == 0:
        raise TreasuryError('zero_amount', 'A money line needs an amount.')
    box = _lock(box)
    if not box.is_active:
        raise TreasuryError('box_closed', 'This money box is closed.', box=box.name)
    if (check_floor and amount < 0 and not box.allow_negative and kind != MoneyMove.Kind.COUNT_ADJUST):
        have = balance(box)
        if have + amount < 0:
            raise TreasuryError('not_enough_money', 'Not enough money in this box.', box=box.name,
                                balance=str(have), needed=str(-amount))
    return MoneyMove.all_objects.create(box=box, part_id=box.part_id, store_id=box.store_id, amount=amount, kind=kind,
                                        at=at or timezone.now(), created_by=user, note=note or '', reason=reason or '',
                                        **links)


@transaction.atomic
def reverse(move, *, user=None, reason='', allow_below_zero=False):
    """The opposite line. Once only; a reversal can't be reversed (write a new line instead). `allow_below_zero` = a
    correction made while the shop's switch is OFF (the business event already happened; the book must follow it)."""
    if not (reason or '').strip():
        raise TreasuryError('reason_required', 'Write why this line is reversed.')
    move = MoneyMove.all_objects.select_for_update().get(pk=move.pk)
    if move.kind == MoneyMove.Kind.REVERSAL:
        raise TreasuryError('cannot_reverse_reversal', 'A reversal line cannot be reversed.')
    if MoneyMove.all_objects.filter(reverses=move).exists():
        raise TreasuryError('already_reversed', 'This line is already reversed.')
    links = {f + '_id': getattr(move, f + '_id') for f in SOURCE_LINKS}
    # Taking back money that came IN must respect the floor; putting back money that went OUT never can fail.
    rev = post(move.box, -move.amount, MoneyMove.Kind.REVERSAL, user=user, reason=reason, reverses=move,
               transfer_id=move.transfer_id, check_floor=move.amount > 0 and not allow_below_zero, **links)
    # A reversed loan / amana line changes what is still owed / held → keep its "closed" mark true.
    if move.loan_id:
        _sync_closed(Loan.all_objects.select_for_update().get(pk=move.loan_id), loan_owed)
    if move.amana_id:
        _sync_closed(Amana.all_objects.select_for_update().get(pk=move.amana_id), amana_held)
    return rev


@transaction.atomic
def transfer(src, dst, amount, *, user=None, note='', at=None):
    """Money moved between two boxes of the SAME owner: never income, never expense. Both lines or none."""
    amount = _q(amount)
    if amount <= 0:
        raise TreasuryError('amount_positive', 'A transfer needs an amount above zero.')
    if src.pk == dst.pk:
        raise TreasuryError('same_box', 'Choose two different boxes.')
    if src.owner_id != dst.owner_id:
        raise TreasuryError('other_owner', 'Both boxes must belong to the same owner.')
    # Lock both rows in one order (no deadlock when two people transfer A→B and B→A at the same moment).
    for b in sorted([src, dst], key=lambda b: str(b.pk)):
        _lock(b)
    tid = uuid.uuid4()
    when = at or timezone.now()
    out = post(src, -amount, MoneyMove.Kind.TRANSFER_OUT, user=user, note=note, at=when, transfer_id=tid)
    inn = post(dst, amount, MoneyMove.Kind.TRANSFER_IN, user=user, note=note, at=when, transfer_id=tid)
    return out, inn


@transaction.atomic
def count(box, counted, *, user=None, reason='', shift=None):
    """Someone counted the box for real. Equal → nothing written (returns None). Different → a COUNT_ADJUST line for
    the difference, with the reason (required)."""
    counted = _q(counted)
    box = _lock(box)
    diff = counted - balance(box)
    if diff == 0:
        return None
    if not (reason or '').strip():
        raise TreasuryError('reason_required', 'The count differs from the book — write the reason.',
                            difference=str(diff))
    return post(box, diff, MoneyMove.Kind.COUNT_ADJUST, user=user, reason=reason, shift=shift)


@transaction.atomic
def open_box(*, part, name, kind, user=None, opening=ZERO, branch=None, note='', allow_negative=None):
    """A new box in `part` (+ one OPENING line when the counted opening amount ≠ 0)."""
    if allow_negative is None:
        allow_negative = kind in NEGATIVE_OK_KINDS
    opening = _q(opening or 0)
    if opening < 0 and not allow_negative:
        raise TreasuryError('not_enough_money', 'A cash box cannot start below zero.')
    box = MoneyBox.all_objects.create(owner=part.owner, part=part, store=part.store, name=name, kind=kind,
                                      branch=branch, note=note, allow_negative=allow_negative)
    if opening != 0:
        post(box, opening, MoneyMove.Kind.OPENING, user=user, check_floor=False)
    return box


# ═══ Wave 2 — the owner level (TREASURY.md § 12) ═════════════════════════════════════════════════════════════════
def _pos(amount, code='amount_positive'):
    amount = _q(amount)
    if amount <= 0:
        raise TreasuryError(code, 'The amount must be above zero.')
    return amount


def _money(amount):
    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


def owner_level_on(store):
    if store is None:
        return False
    from core.models import StoreSettings
    row = StoreSettings.objects.filter(store=store).values('treasury_enabled', 'treasury_owner_level').first()
    return bool(row and row['treasury_enabled'] and row['treasury_owner_level'])


def owner_for_user(user):
    """The Owner this login IS — only the person behind their shop's part (Owner.users holds the login). Another
    OWNER-role login of the same shop is NOT the person; staff and sudo never are. None = no owner level for them."""
    store = getattr(user, 'store', None)
    if store is None or getattr(user, 'role', None) != 'OWNER':
        return None
    part = OwnerPart.all_objects.filter(store=store, is_deleted=False).select_related('owner').first()
    if part is None or not part.owner.users.filter(pk=user.pk).exists():
        return None
    return part.owner


def personal_part(owner):
    part = OwnerPart.all_objects.filter(owner=owner, kind=OwnerPart.Kind.PERSONAL, is_deleted=False).first()
    return part or OwnerPart.all_objects.create(owner=owner, kind=OwnerPart.Kind.PERSONAL, name='')


@transaction.atomic
def setup_owner_level(store):
    """The owner level switched on: the owner's ONE personal part (idempotent). Business parts are made by the owner."""
    return personal_part(shop_part(store).owner)


def owner_parts(owner):
    """The owner's live parts they may see: personal + businesses + the SHOP part of every shop with the Treasury on."""
    from core.models import StoreSettings
    shops_on = StoreSettings.objects.filter(treasury_enabled=True).values('store_id')
    return (OwnerPart.all_objects.filter(owner=owner, is_deleted=False)
            .filter(Q(store__isnull=True) | Q(store_id__in=shops_on)))


def owner_boxes(owner):
    return MoneyBox.all_objects.filter(owner=owner, part__in=owner_parts(owner))


def _check_box_in_part(box, part):
    if box.part_id != part.pk:
        raise TreasuryError('box_other_part', 'Choose a box of the same part.', box=box.name)


# ── moving money between parts ────────────────────────────────────────────────────────────────────────────────
def pair_kinds(src_part, dst_part):
    """(out kind, in kind, label). Same part = a transfer. Leaving a business = an owner draw; arriving in a business =
    an owner put-in; business → business = both (it left one, it entered the other). Never income, never expense."""
    K = MoneyMove.Kind
    if src_part.pk == dst_part.pk:
        return K.TRANSFER_OUT, K.TRANSFER_IN, 'TRANSFER'
    src_biz, dst_biz = src_part.kind in BUSINESS_PARTS, dst_part.kind in BUSINESS_PARTS
    if src_biz and dst_biz:
        return K.DRAW_OUT, K.PUT_IN, 'BETWEEN'
    if src_biz:
        return K.DRAW_OUT, K.DRAW_IN, 'DRAW'
    if dst_biz:
        return K.PUT_OUT, K.PUT_IN, 'PUT_IN'
    return K.TRANSFER_OUT, K.TRANSFER_IN, 'TRANSFER'


@transaction.atomic
def move_between(src, dst, amount, *, user=None, note='', at=None):
    """Money moved between two boxes of the SAME owner, any parts → (out line, in line, label). Both lines or none."""
    amount = _pos(amount)
    if src.pk == dst.pk:
        raise TreasuryError('same_box', 'Choose two different boxes.')
    if src.owner_id != dst.owner_id:
        raise TreasuryError('other_owner', 'Both boxes must belong to the same owner.')
    for b in sorted([src, dst], key=lambda b: str(b.pk)):
        _lock(b)
    out_kind, in_kind, label = pair_kinds(src.part, dst.part)
    tid = uuid.uuid4()
    when = at or timezone.now()
    out = post(src, -amount, out_kind, user=user, note=note, at=when, transfer_id=tid)
    inn = post(dst, amount, in_kind, user=user, note=note, at=when, transfer_id=tid)
    return out, inn, label


# ── loans ─────────────────────────────────────────────────────────────────────────────────────────────────────
def loan_owed(loan):
    """Always ≥ 0 in normal use. BORROWED: what we still owe. LENT: what they still owe us."""
    s = MoneyMove.all_objects.filter(loan=loan).aggregate(s=Sum('amount'))['s'] or ZERO
    return s if loan.direction == Loan.Direction.BORROWED else -s


def _sync_closed(obj, owed_fn):
    closed = owed_fn(obj) == 0
    if obj.is_closed != closed:
        obj.is_closed = closed
        obj.save(update_fields=['is_closed', 'updated_at'])


def _loan_line(loan, amount, box, *, start, user, note, at):
    K = MoneyMove.Kind
    if loan.direction == Loan.Direction.BORROWED:
        kind, signed = (K.LOAN_IN, amount) if start else (K.LOAN_REPAY, -amount)
    else:
        kind, signed = (K.LOAN_OUT, -amount) if start else (K.LOAN_BACK, amount)
    return post(box, signed, kind, user=user, note=note, at=at, loan=loan)


@transaction.atomic
def loan_open(part, direction, counterparty, amount, box, *, user=None, note='', at=None):
    amount = _pos(amount)
    if direction not in Loan.Direction.values:
        raise TreasuryError('bad_direction', 'Choose borrowed or lent.')
    if not (counterparty or '').strip():
        raise TreasuryError('name_required', 'Write who the loan is with.')
    _check_box_in_part(box, part)
    loan = Loan.objects.create(owner=part.owner, part=part, direction=direction, counterparty=counterparty.strip(),
                               note=note or '', opened_on=timezone.localdate(at) if at else timezone.localdate())
    _loan_line(loan, amount, box, start=True, user=user, note=note, at=at)
    return loan


@transaction.atomic
def loan_more(loan, amount, box, *, user=None, note='', at=None):
    """More money on the same loan (borrowed more / lent more)."""
    amount = _pos(amount)
    loan = Loan.objects.select_for_update().get(pk=loan.pk)
    _check_box_in_part(box, loan.part)
    line = _loan_line(loan, amount, box, start=True, user=user, note=note, at=at)
    _sync_closed(loan, loan_owed)
    return line


@transaction.atomic
def loan_pay(loan, amount, box, *, user=None, note='', at=None):
    """Borrowed: we pay back (LOAN_REPAY). Lent: they pay us back (LOAN_BACK). Never above what is still owed."""
    amount = _pos(amount)
    loan = Loan.objects.select_for_update().get(pk=loan.pk)
    _check_box_in_part(box, loan.part)
    owed = loan_owed(loan)
    if amount > owed:
        raise TreasuryError('loan_overpaid', 'That is more than is still owed on this loan.', owed=str(owed))
    line = _loan_line(loan, amount, box, start=False, user=user, note=note, at=at)
    _sync_closed(loan, loan_owed)
    return line


# ── amanat (money held for others) ────────────────────────────────────────────────────────────────────────────
def amana_held(amana):
    return MoneyMove.all_objects.filter(amana=amana).aggregate(s=Sum('amount'))['s'] or ZERO


def amanat_total(owner):
    """The live total the owner holds for other people (every part, every box)."""
    return (MoneyMove.all_objects.filter(amana__owner=owner, amana__is_deleted=False)
            .aggregate(s=Sum('amount'))['s'] or ZERO)


@transaction.atomic
def amana_receive(part, held_for, amount, box, *, user=None, note='', at=None):
    amount = _pos(amount)
    if not (held_for or '').strip():
        raise TreasuryError('name_required', 'Write who the money is held for.')
    _check_box_in_part(box, part)
    amana = Amana.objects.create(owner=part.owner, part=part, held_for=held_for.strip(), note=note or '',
                                 opened_on=timezone.localdate(at) if at else timezone.localdate())
    post(box, amount, MoneyMove.Kind.AMANA_IN, user=user, note=note, at=at, amana=amana)
    return amana


@transaction.atomic
def amana_more(amana, amount, box, *, user=None, note='', at=None):
    amount = _pos(amount)
    amana = Amana.objects.select_for_update().get(pk=amana.pk)
    _check_box_in_part(box, amana.part)
    line = post(box, amount, MoneyMove.Kind.AMANA_IN, user=user, note=note, at=at, amana=amana)
    _sync_closed(amana, amana_held)
    return line


@transaction.atomic
def amana_return(amana, amount, box, *, user=None, note='', at=None):
    amount = _pos(amount)
    amana = Amana.objects.select_for_update().get(pk=amana.pk)
    _check_box_in_part(box, amana.part)
    held = amana_held(amana)
    if amount > held:
        raise TreasuryError('amana_overdrawn', 'That is more than is held for this person.', held=str(held))
    line = post(box, -amount, MoneyMove.Kind.AMANA_OUT, user=user, note=note, at=at, amana=amana)
    _sync_closed(amana, amana_held)
    return line


# ── partners ──────────────────────────────────────────────────────────────────────────────────────────────────
def check_shares(part, extra_pct=ZERO, exclude=None):
    """Σ active partner shares of `part` (+ `extra_pct`, minus `exclude`) must stay ≤ 100. Locks the part row so two
    owners' tabs can't both add a partner past 100. Call inside a transaction."""
    if part.kind not in BUSINESS_PARTS:
        raise TreasuryError('partners_business_only', 'Partners belong to a shop or a business, not personal money.')
    OwnerPart.all_objects.select_for_update().get(pk=part.pk)
    qs = Partner.objects.filter(part=part, is_active=True)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    total = (qs.aggregate(s=Sum('share_pct'))['s'] or ZERO) + Decimal(str(extra_pct or 0))
    if total > 100:
        raise TreasuryError('share_total_too_high', 'All partner shares together can be 100% at most.', total=str(total))
    return total


def partner_account(partner):
    """What the business owes the partner: their settled shares + what they put in − what they were paid.
    Negative = the partner owes the business (paid ahead of their share, or a loss)."""
    settled = (PartnerSettlementLine.objects.filter(partner=partner, settlement__cancelled_at__isnull=True)
               .aggregate(s=Sum('amount'))['s'] or ZERO)
    moved = MoneyMove.all_objects.filter(partner=partner).aggregate(s=Sum('amount'))['s'] or ZERO
    return settled + moved


@transaction.atomic
def partner_put_in(partner, amount, box, *, user=None, note='', at=None):
    amount = _pos(amount)
    _check_box_in_part(box, partner.part)
    return post(box, amount, MoneyMove.Kind.PARTNER_IN, user=user, note=note, at=at, partner=partner)


@transaction.atomic
def partner_pay(partner, amount, box, *, user=None, note='', at=None):
    amount = _pos(amount)
    _check_box_in_part(box, partner.part)
    return post(box, -amount, MoneyMove.Kind.PARTNER_OUT, user=user, note=note, at=at, partner=partner)


# ── profit per business + settlements ─────────────────────────────────────────────────────────────────────────
def part_profit(part, date_from, date_to):
    """{profit, owner_only_expenses} of a SHOP / BUSINESS part for [date_from, date_to] (local dates).
    SHOP = the shop P&L net (reports.views.compute_pnl — the very same maths as the P&L report).
    BUSINESS = its income − its spending (PartEntry). PERSONAL money is never profit."""
    if part.kind == OwnerPart.Kind.SHOP:
        from core.tenancy import get_current_store
        from finance.models import Expense
        from reports.views import compute_pnl
        current = get_current_store()
        if current is not None and current.pk != part.store_id:
            # The shop managers scope to the signed-in shop; another shop's figures would read as zero.
            raise TreasuryError('switch_shop', "Sign in to that shop to work with its profit.")
        _rows, totals = compute_pnl(part.store, date_from, date_to, 'month')
        owner_only = (Expense.all_objects.filter(store=part.store, is_deleted=False, owner_only=True,
                                                 date__gte=date_from, date__lte=date_to)
                      .aggregate(s=Sum('amount'))['s'] or ZERO)
        return {'profit': Decimal(totals['net']), 'owner_only_expenses': owner_only}
    if part.kind == OwnerPart.Kind.BUSINESS:
        live = PartEntry.objects.filter(part=part, date__gte=date_from, date__lte=date_to)
        inn = live.filter(direction=PartEntry.Direction.IN).aggregate(s=Sum('amount'))['s'] or ZERO
        out = live.filter(direction=PartEntry.Direction.OUT).aggregate(s=Sum('amount'))['s'] or ZERO
        owner_only = (live.filter(direction=PartEntry.Direction.OUT, owner_only=True)
                      .aggregate(s=Sum('amount'))['s'] or ZERO)
        return {'profit': inn - out, 'owner_only_expenses': owner_only}
    raise TreasuryError('personal_no_pnl', 'Personal money has no profit and loss.')


def settlement_preview(part, date_from, date_to):
    """The split, nothing saved. Partners share `profit + owner-only expenses` (they don't carry those); the owner gets
    the rest — so the owner carries the owner-only expenses and any rounding cent. A loss is shared the same way."""
    if date_from > date_to:
        raise TreasuryError('bad_dates', 'The end date is before the start date.')
    fig = part_profit(part, date_from, date_to)
    profit, owner_only = _money(fig['profit']), _money(fig['owner_only_expenses'])
    base = profit + owner_only
    partners = list(Partner.objects.filter(part=part, is_active=True).order_by('name', 'id'))
    lines, given, pct_sum = [], ZERO, ZERO
    for p in partners:
        amount = _money(base * p.share_pct / 100)
        lines.append({'partner': p, 'share_pct': p.share_pct, 'amount': amount})
        given += amount
        pct_sum += p.share_pct
    return {'profit': profit, 'owner_only_expenses': owner_only, 'base': base, 'lines': lines,
            'owner_share_pct': Decimal('100') - pct_sum, 'owner_amount': profit - given}


@transaction.atomic
def settle(part, date_from, date_to, *, user=None):
    part = OwnerPart.all_objects.select_for_update().get(pk=part.pk)
    if part.kind not in BUSINESS_PARTS:
        raise TreasuryError('personal_no_pnl', 'Personal money has no profit and loss.')
    if date_to > timezone.localdate():
        raise TreasuryError('settlement_future', 'A period can be settled only once it has ended.')
    if not Partner.objects.filter(part=part, is_active=True).exists():
        raise TreasuryError('no_partners', 'This part has no partners to settle with.')
    if PartnerSettlement.objects.filter(part=part, cancelled_at__isnull=True, date_from__lte=date_to,
                                        date_to__gte=date_from).exists():
        raise TreasuryError('settlement_overlap', 'Part of this period is already settled — cancel that one first.')
    fig = settlement_preview(part, date_from, date_to)
    s = PartnerSettlement.objects.create(owner=part.owner, part=part, date_from=date_from, date_to=date_to,
                                         profit=fig['profit'], owner_only_expenses=fig['owner_only_expenses'],
                                         created_by=user)
    PartnerSettlementLine.objects.bulk_create(
        [PartnerSettlementLine(settlement=s, partner=l['partner'], share_pct=l['share_pct'], amount=l['amount'])
         for l in fig['lines']]
        + [PartnerSettlementLine(settlement=s, partner=None, share_pct=fig['owner_share_pct'],
                                 amount=fig['owner_amount'])])
    return s


@transaction.atomic
def cancel_settlement(settlement, *, user=None, reason=''):
    if not (reason or '').strip():
        raise TreasuryError('reason_required', 'Write why this settlement is cancelled.')
    s = PartnerSettlement.objects.select_for_update().get(pk=settlement.pk)
    if s.cancelled_at:
        raise TreasuryError('already_cancelled', 'This settlement is already cancelled.')
    s.cancelled_at = timezone.now()
    s.cancel_reason = reason.strip()
    s.save(update_fields=['cancelled_at', 'cancel_reason', 'updated_at'])
    return s
