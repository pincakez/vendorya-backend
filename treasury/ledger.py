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
"""
import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Owner, OwnerPart, MoneyBox, MoneyMove

ZERO = Decimal('0')
CENT = Decimal('0.01')

# Kinds that may hold a negative balance by default (an overdraft); every other kind is cash-like.
NEGATIVE_OK_KINDS = {MoneyBox.Kind.BANK}


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
def reverse(move, *, user=None, reason=''):
    """The opposite line. Once only; a reversal can't be reversed (write a new line instead)."""
    if not (reason or '').strip():
        raise TreasuryError('reason_required', 'Write why this line is reversed.')
    move = MoneyMove.all_objects.select_for_update().get(pk=move.pk)
    if move.kind == MoneyMove.Kind.REVERSAL:
        raise TreasuryError('cannot_reverse_reversal', 'A reversal line cannot be reversed.')
    if MoneyMove.all_objects.filter(reverses=move).exists():
        raise TreasuryError('already_reversed', 'This line is already reversed.')
    links = {f: getattr(move, f + '_id') for f in ('payment', 'customer_receipt', 'refund', 'supplier_payment',
                                                    'expense', 'other_income', 'shift')}
    # Taking back money that came IN must respect the floor; putting back money that went OUT never can fail.
    return post(move.box, -move.amount, MoneyMove.Kind.REVERSAL, user=user, reason=reason, reverses=move,
                transfer_id=move.transfer_id, check_floor=move.amount > 0, **links)


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
