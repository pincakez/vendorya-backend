"""The money flows → the Treasury Ledger's boxes (TREASURY.md § 4 + § 11 part B).

NEW lines are written only while the shop's Treasury is on (`ledger.is_on`): a shop that never turned it on gets
today's Vendorya exactly. ON: each business event writes its box line(s) through `treasury.ledger` (the ONE door) inside
the caller's transaction, so a refused line (e.g. a cash box that would go below zero) rolls the whole event back. A
refusal reaches the API as a clean 400 `{"code", "detail", ...params}` (`treasury_400`).
CORRECTIONS always happen: an edit / delete / void of something that already has a live line reverses it even while the
switch is off (then without the below-zero check, and without writing a new line) — so turning it off and on never
leaves a box wrong.

Where money lands (one rule, `_box_for`): cash → the drawer of the person's open shift (any branch — one per person),
else the default drawer of the event's branch; card / e-wallet / bank → the method's "lands in" box, else the shop's "not sorted yet" box; Ajel and
store credit → no line (no money moved).
"""
from contextlib import contextmanager
from decimal import Decimal

from django.db.models import Sum
from rest_framework.exceptions import APIException

from treasury import ledger
from treasury.ledger import TreasuryError
from treasury.models import MoneyBox, MoneyMove

ZERO = Decimal('0')
CENT = Decimal('0.01')
K = MoneyMove.Kind


# ── errors ─────────────────────────────────────────────────────────────────────────────────────────────────────
class TreasuryRefused(APIException):
    """A 400 with a FLAT body `{"code", "detail", ...}` (strings, not lists). Not a ValidationError on purpose: raised
    inside a serializer's validate(), DRF would wrap every value in a list; this one passes through unchanged."""
    status_code = 400
    default_code = 'treasury'


def treasury_400(exc):
    body = {'code': exc.code, 'detail': str(exc)}
    body.update({k: (None if v is None else str(v)) for k, v in exc.params.items()})
    return TreasuryRefused(body)


def refuse(code, detail, **params):
    raise treasury_400(TreasuryError(code, detail, **params))


@contextmanager
def guarded():
    """A TreasuryError inside → a 400 the UI translates by `code` (the caller's atomic block rolls back)."""
    try:
        yield
    except TreasuryError as exc:
        raise treasury_400(exc) from exc


# ── where money lands ──────────────────────────────────────────────────────────────────────────────────────────
def open_shift_of(user, store, branch=None):
    """The person's OPEN shift (in `branch` when given) — one per person at most (DB rule)."""
    from .models import WorkShift
    if user is None or store is None:
        return None
    qs = WorkShift.all_objects.select_related('drawer').filter(store=store, user=user, status=WorkShift.Status.OPEN)
    if branch is not None:
        qs = qs.filter(branch=branch)
    return qs.first()


def user_branch(user, store):
    """The branch a branch-less event (a customer receipt, a supplier payment) happens in: the person's open shift's
    branch, else their default branch, else the shop's first branch."""
    from core.models import Branch
    shift = open_shift_of(user, store)
    if shift is not None:
        return shift.branch
    b = getattr(user, 'default_branch', None)
    if b is not None and b.store_id == store.id and not b.is_deleted:
        return b
    return Branch.objects.filter(store=store, is_deleted=False).order_by('created_at').first()


def _default_drawer(store, branch):
    drawer = ledger.default_drawer(branch)
    if drawer is None and branch is not None:
        ledger.setup(store)                       # a branch added after the switch was turned on gets its drawer now
        drawer = ledger.default_drawer(branch)
    if drawer is None:
        raise TreasuryError('no_drawer', 'This branch has no open till drawer.')
    return drawer


def _box_for(method, store, *, shift=None, branch=None):
    """`ledger.box_for_method`, never silent: a cash payment always finds a drawer (or is refused)."""
    if method is None or method.is_agel or getattr(method, 'is_store_credit', False):
        return None
    if method.is_cash:
        if shift is not None and shift.drawer_id:
            return shift.drawer
        return _default_drawer(store, branch if branch is not None else getattr(shift, 'branch', None))
    return ledger.box_for_method(method, shift=shift, branch=branch)


def _shift_link(box, shift):
    """A line is tagged with a shift only when it lands in that shift's drawer."""
    return shift if (shift is not None and box is not None and shift.drawer_id == box.pk) else None


def _live(**filters):
    """Lines not reversed yet (and not reversals themselves)."""
    return (MoneyMove.all_objects.filter(**filters).exclude(kind=K.REVERSAL)
            .filter(reversed_by__isnull=True).order_by('at', 'created_at'))


def _reverse_all(qs, user, reason, store=None):
    """Reverse every live line in `qs`. A correction always happens: with the switch ON the cash floor applies (a
    drawer that no longer holds the money refuses → the event is undone); with it OFF the event is not ours to block,
    so the book just follows it (it may show below zero — honest: money left a box nobody was tracking)."""
    lines = list(qs)
    if not lines:
        return
    off = not ledger.is_on(store if store is not None else lines[0].store)
    for move in lines:
        ledger.reverse(move, user=user, reason=reason, allow_below_zero=off)


def check_box(box, store, field):
    """A box picked in a form must be one of THIS shop's open boxes (tenant guard + closed boxes)."""
    if box is None:
        return
    if box.store_id != getattr(store, 'id', None):
        refuse('box_not_found', 'This money box was not found in your shop.', field=field)
    if not box.is_active:
        refuse('box_closed', 'This money box is closed.', field=field)


# ── sales ──────────────────────────────────────────────────────────────────────────────────────────────────────
def post_sale_payment(payment):
    """One SALE_IN per Payment (the money KEPT — change is never stored). A Payment that is a share of a
    CustomerReceipt is skipped: the receipt posts once, for its whole amount."""
    invoice = payment.invoice
    store = invoice.store
    if not ledger.is_on(store) or payment.receipt_id:
        return None
    with guarded():
        # The payer's open shift in ANY branch (one per person) — the drawer that really took the cash; the sale's
        # branch default drawer only when they have no open shift.
        shift = open_shift_of(payment.created_by, store)
        box = _box_for(payment.method, store, shift=shift, branch=invoice.branch)
        if box is None:
            return None
        return ledger.post(box, payment.amount, K.SALE_IN, user=payment.created_by, payment=payment,
                           shift=_shift_link(box, shift))


def reverse_sale(invoice, user):
    """A POSTED sale is voided: the money it took goes back out of the boxes it went into (the payments stay as
    they are — the sale's history is not rewritten). Receipt shares have no line of their own (see post_receipt)."""
    with guarded():
        _reverse_all(_live(payment__invoice=invoice, kind=K.SALE_IN), user, 'Sale voided', invoice.store)


# ── customer receipts (a customer pays a debt) ─────────────────────────────────────────────────────────────────
def post_receipt(receipt):
    store = receipt.store
    if not ledger.is_on(store):
        return None
    with guarded():
        user = receipt.created_by
        shift = open_shift_of(user, store)
        branch = shift.branch if shift is not None else user_branch(user, store)
        box = _box_for(receipt.method, store, shift=shift, branch=branch)
        if box is None:
            return None
        return ledger.post(box, receipt.amount, K.RECEIPT_IN, user=user, customer_receipt=receipt,
                           shift=_shift_link(box, shift))


def reverse_receipt(receipt, user):
    with guarded():
        _reverse_all(_live(customer_receipt=receipt, kind=K.RECEIPT_IN), user, 'Customer payment deleted',
                     receipt.store)


# ── refunds ────────────────────────────────────────────────────────────────────────────────────────────────────
def _payment_box(payment, store, branch):
    """The box that really took this payment's money: its own SALE_IN line, else its receipt's line, else where its
    method lands today."""
    line = MoneyMove.all_objects.filter(payment=payment, kind=K.SALE_IN).select_related('box').first()
    if line is None and payment.receipt_id:
        line = (MoneyMove.all_objects.filter(customer_receipt_id=payment.receipt_id, kind=K.RECEIPT_IN)
                .select_related('box').first())
    if line is not None:
        return line.box
    return _box_for(payment.method, store, branch=branch)


def _non_cash_sources(refund, original, need):
    """Split the non-cash part of a payout over the original sale's card / e-wallet / bank payments, in the order
    they were taken. Earlier returns are taken off the front of the same queue first, so each return gets its own
    slice and the sum never exceeds what those methods took."""
    from .models import Payment, RefundInvoice
    store = refund.store
    if original is None:
        return [(ledger.unsorted_box(store), need)]
    earlier = ZERO
    for r in (RefundInvoice.all_objects.filter(original_invoice=original, is_deleted=False,
                                               refund_method=RefundInvoice.RefundMethod.CASH)
              .exclude(pk=refund.pk)):
        earlier += max((r.paid_back or ZERO) - (r.store_credit_back or ZERO) - (r.drawer_cash or ZERO), ZERO)
    pays = (Payment.all_objects.filter(invoice=original, is_deleted=False)
            .exclude(method__is_cash=True).exclude(method__is_store_credit=True).exclude(method__is_agel=True)
            .select_related('method').order_by('created_at', 'id'))
    out = {}
    for p in pays:
        if need <= 0:
            break
        avail = p.amount
        skip = min(earlier, avail)
        avail, earlier = avail - skip, earlier - skip
        if avail <= 0:
            continue
        take = min(avail, need)
        box = _payment_box(p, store, original.branch)
        if box is None:
            continue
        out[box] = out.get(box, ZERO) + take
        need -= take
    if need > 0:   # can't be traced to a method (never expected) — honest, visible in "not sorted yet"
        box = ledger.unsorted_box(store)
        out[box] = out.get(box, ZERO) + need
    return list(out.items())


def post_refund(refund, original, user):
    """REFUND_OUT: the cash part (`drawer_cash`) from the refund shift's drawer (else the branch's default drawer); the
    non-cash part of `paid_back` from the box(es) of the method(s) that took the money. Store credit → no line."""
    store = refund.store
    if not ledger.is_on(store):
        return
    from .models import RefundInvoice
    with guarded():
        cash = refund.drawer_cash or ZERO
        if cash > 0:
            shift = refund.shift
            box = shift.drawer if (shift is not None and shift.drawer_id) else _default_drawer(store, refund.branch)
            ledger.post(box, -cash, K.REFUND_OUT, user=user, refund=refund, shift=_shift_link(box, shift))
        if refund.refund_method != RefundInvoice.RefundMethod.CASH:
            return
        other = (refund.paid_back or ZERO) - (refund.store_credit_back or ZERO) - cash
        if other > 0:
            for box, amount in _non_cash_sources(refund, original, other):
                ledger.post(box, -amount, K.REFUND_OUT, user=user, refund=refund)


def reverse_refund(refund, user):
    """A return is deleted (soft): the money it paid out comes back into the same boxes."""
    with guarded():
        _reverse_all(_live(refund=refund, kind=K.REFUND_OUT), user, 'Return deleted', refund.store)


# ── money paid out from a chosen box (expense, supplier payment) ───────────────────────────────────────────────
def default_box_for_supplier(method, user, store):
    """Where a supplier payment comes from when the form doesn't say: the method's box (cash → my open drawer)."""
    if method is None:
        return None
    shift = open_shift_of(user, store)
    try:
        return _box_for(method, store, shift=shift, branch=shift.branch if shift else user_branch(user, store))
    except TreasuryError:
        return None


def has_line(obj, kind, link):
    return _live(**{link: obj}, kind=kind).exists()


def sync_paid_out(obj, *, kind, link, user, created=False, reason_edit='Edited', reason_delete='Deleted'):
    """Keep ONE live line for an expense / supplier payment: created → post; amount or box changed → reverse the old
    line + post the new one; deleted (soft) → reverse. A row made before the Treasury (no line) is never put in a box.
    Switch OFF: an existing line is still corrected (reversed on delete or on a changed amount) but no new line is
    written — the row then stays out of the boxes."""
    store = obj.store
    on = ledger.is_on(store)
    with guarded():
        live = list(_live(**{link: obj}, kind=kind))
        if obj.is_deleted:
            _reverse_all(live, user, reason_delete, store)
            return
        if live:
            if len(live) == 1 and live[0].box_id == obj.paid_from_id and -live[0].amount == obj.amount:
                return
            _reverse_all(live, user, reason_edit, store)
            if not on:
                return
        elif not (created and on):
            return                                      # made before the Treasury / while it is off: no box
        if obj.paid_from_id is None:
            raise TreasuryError('paid_from_required', 'Choose the money box this was paid from.')
        box = obj.paid_from
        shift = open_shift_of(user, store)
        ledger.post(box, -obj.amount, kind, user=user, shift=_shift_link(box, shift), **{link: obj})


# ── shifts (the drawer is a box) ───────────────────────────────────────────────────────────────────────────────
def carried_over(shift):
    """The drawer's book balance the moment the shift opened (before the opening count)."""
    if shift.ledger != shift.Ledger.BOX or not shift.drawer_id:
        return None
    return ledger.balance(shift.drawer, at=shift.start_time).quantize(CENT)


def drawer_lines(shift):
    """The drawer's lines since the shift opened, minus the shift's own opening count (already in starting_cash)."""
    return (MoneyMove.all_objects.filter(box_id=shift.drawer_id, at__gt=shift.start_time)
            .exclude(kind=K.COUNT_ADJUST, shift=shift)
            .select_related('payment__invoice', 'refund__original_invoice', 'customer_receipt__customer')
            .order_by('-at', '-created_at'))


def drawers_with_balance(store, branch=None, show_who=False):
    """The open-shift picker's drawers. `show_who` (MANAGER+) adds WHO holds a busy drawer; staff below see only
    that it is busy."""
    from .models import WorkShift
    qs = MoneyBox.all_objects.filter(store=store, kind=MoneyBox.Kind.DRAWER, is_active=True).select_related('branch')
    if branch is not None:
        qs = qs.filter(branch=branch)
    boxes = list(qs.order_by('-is_default_drawer', 'name'))
    sums = dict(MoneyMove.all_objects.filter(box__in=boxes).values('box').annotate(s=Sum('amount'))
                .values_list('box', 's'))
    busy = {s.drawer_id: s for s in WorkShift.all_objects.select_related('user')
            .filter(store=store, status=WorkShift.Status.OPEN, drawer__in=boxes)}
    out = []
    for b in boxes:
        s = busy.get(b.pk)
        row = {
            'id': str(b.pk), 'name': b.name, 'branch': str(b.branch_id) if b.branch_id else None,
            'branch_name': b.branch.name if b.branch_id else None, 'is_default_drawer': b.is_default_drawer,
            'book_balance': str((sums.get(b.pk) or ZERO).quantize(CENT)),
            'busy': s is not None,
        }
        if show_who:
            row['busy_by'] = (s.user.get_full_name() or s.user.username) if s is not None else None
            row['busy_shift'] = str(s.pk) if s is not None else None
        out.append(row)
    return out


# ── close: "move the takings to …" ─────────────────────────────────────────────────────────────────────────────
# Where takings may go at close, by the closer's role (below MANAGER: cash-like boxes only; MANAGER+: + bank / e-wallet).
MOVE_KINDS_BELOW_MANAGER = {MoneyBox.Kind.CASH, MoneyBox.Kind.RESERVED, MoneyBox.Kind.SAVINGS}
MOVE_KINDS_MANAGER = MOVE_KINDS_BELOW_MANAGER | {MoneyBox.Kind.BANK, MoneyBox.Kind.WALLET}


def is_manager(user):
    from users.models import User
    from users.permissions import ROLE_RANK, _user_rank
    return _user_rank(user) >= ROLE_RANK[User.Role.MANAGER]


def check_move_target(box, store, user):
    """The box a closing cashier moves the takings to: one of THIS shop's open boxes, never a drawer (nor any box with
    an open shift), never the "not sorted yet" box, and only the kinds the closer's role may move money into."""
    if box is None or not _move_target_ok(box, store, user, _busy_box_ids(store)):
        refuse('move_target_not_allowed', "The takings can't be moved to this box.")


def _busy_box_ids(store):
    from .models import WorkShift
    return set(WorkShift.all_objects.filter(store=store, status=WorkShift.Status.OPEN, drawer__isnull=False)
               .values_list('drawer_id', flat=True))


def _move_target_ok(box, store, user, busy_ids):
    allowed = MOVE_KINDS_MANAGER if is_manager(user) else MOVE_KINDS_BELOW_MANAGER
    return (box.store_id == getattr(store, 'id', None) and box.is_active
            and box.kind != MoneyBox.Kind.DRAWER and box.system_role != MoneyBox.Role.UNSORTED
            and box.kind in allowed and box.pk not in busy_ids)


def move_targets(store, user):
    """The boxes THIS caller may move takings to at close (the same rule as check_move_target). Balances only for
    MANAGER+ (a cashier never sees what the safe or the bank holds)."""
    busy = _busy_box_ids(store)
    boxes = [b for b in MoneyBox.all_objects.filter(store=store, is_active=True).order_by('kind', 'name')
             if _move_target_ok(b, store, user, busy)]
    show = is_manager(user)
    return [{'id': str(b.pk), 'name': b.name, 'kind': b.kind,
             **({'balance': str(ledger.balance(b).quantize(CENT))} if show else {})} for b in boxes]


# ── sudo trash: restoring a deleted money record ───────────────────────────────────────────────────────────────
_LINK_FOR = {'Expense': 'expense', 'RefundInvoice': 'refund', 'SupplierPayment': 'supplier_payment',
             'CustomerReceipt': 'customer_receipt', 'OtherIncome': 'other_income'}


def restore_blocked(obj):
    """A deleted expense / return / supplier payment / customer receipt / other income must not come back by an
    un-delete: its box line was reversed when it was deleted, so a restore would leave the books wrong. Blocked when
    the shop's Treasury is on, or the row ever had a box line."""
    link = _LINK_FOR.get(type(obj).__name__)
    if link is None:
        return False
    return ledger.is_on(getattr(obj, 'store', None)) or MoneyMove.all_objects.filter(**{link: obj}).exists()
