"""Reservations with a deposit (s172, RESERVATIONS.md) — the ONE door for every state change, each atomic.

OPEN ──convert──▶ CONVERTED (a POSTED sale; the deposits are its first payments)
OPEN ──cancel───▶ CANCELLED (deposit left: given back / kept = other income / moved to store credit)

While OPEN the lines are HELD: the shelf count does not move, but the till's "available" leaves them out
(finance/checkout.py `held_qty`) and a serial named here can't go on another sale. A deposit is real money the minute
it is taken: Treasury on → a DEPOSIT_IN box line (cash → the taker's drawer); off → a cash deposit counts in the taker's
shift (WorkShift.cash_in). Converting makes `Payment(deposit_of=…)` rows on the sale — they write NO box line and no
shift counts them again (the money was counted when it came in).
"""
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventory.models import ProductVariant, StockLevel
from users.models import Customer
from .models import (Payment, PaymentMethod, Reservation, ReservationDeposit, ReservationItem, ReservationSequence,
                     SalesInvoice, SalesInvoiceItem, person_name)

ZERO = Decimal('0')
CENT = Decimal('0.01')


def _err(code, detail, **extra):
    return ValidationError({'code': code, 'detail': detail, **{k: str(v) for k, v in extra.items()}})


def is_on(store):
    return bool(getattr(getattr(store, 'settings', None), 'reservations_enabled', False))


def _money(raw, code='bad_amount'):
    try:
        val = Decimal(str(raw)).quantize(CENT)
    except (InvalidOperation, TypeError, ValueError):
        raise _err(code, 'Write the amount.')
    if not val.is_finite() or val <= 0:
        raise _err(code, 'The amount must be above zero.')
    return val


def _qty(raw):
    try:
        val = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise _err('bad_quantity', 'Write the quantity.')
    if not val.is_finite() or val <= 0:
        raise _err('bad_quantity', 'The quantity must be above zero.')
    return val


def _deposit_method(store, method):
    """A deposit moves real money: one of the shop's own methods — never Ajel (no money) or store credit."""
    if method is None or method.store_id != store.id or method.is_deleted:
        raise _err('method_required', 'Choose how the deposit is paid.')
    if method.is_agel or method.is_store_credit:
        raise _err('method_not_money', 'A deposit is real money — not Ajel or store credit.')
    return method


def _lock(res):
    return Reservation.all_objects.select_for_update().get(pk=res.pk)


def _must_be_open(res):
    if res.status != Reservation.Status.OPEN:
        raise _err('not_open', 'This reservation is already closed.')


# ── money in / out (box line or shift) ─────────────────────────────────────────────────────────────────────────────
def _post_box_line(dep, *, box=None):
    """Treasury on: DEPOSIT_IN (+) / DEPOSIT_OUT (−) in the box the money really went to / came from."""
    from treasury import ledger
    from treasury.models import MoneyMove
    from .treasury_flows import _box_for, _shift_link, check_box, guarded, user_branch
    store = dep.reservation.store
    if not ledger.is_on(store):
        return None
    with guarded():
        shift = dep.shift
        if box is None:
            branch = shift.branch if shift is not None else (dep.reservation.branch or user_branch(dep.created_by, store))
            box = _box_for(dep.method, store, shift=shift, branch=branch)
        else:
            check_box(box, store, 'box')
        if box is None:
            return None
        kind = MoneyMove.Kind.DEPOSIT_IN if dep.amount > 0 else MoneyMove.Kind.DEPOSIT_OUT
        return ledger.post(box, dep.amount, kind, user=dep.created_by, reservation_deposit=dep,
                           shift=_shift_link(box, shift), note=f'Reservation {dep.reservation.number}')


def _record(res, user, amount, method, note='', box=None):
    from .treasury_flows import open_shift_of
    shift = open_shift_of(user, res.store)
    dep = ReservationDeposit.objects.create(reservation=res, amount=amount, method=method, created_by=user,
                                            shift=shift, note=(note or '')[:200])
    _post_box_line(dep, box=box)
    return dep


# ── create ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _price_total(store, lines):
    """What the customer will pay for these lines (tax included) — the sale pricing rule, nothing saved."""
    from .serializers import SalesInvoiceSerializer
    priced = SalesInvoiceSerializer.price_lines(SimpleNamespace(store=store, discount=ZERO),
                                                [(v, q, p, ZERO) for v, q, p in lines])
    return Decimal(priced['grand_total']).quantize(CENT)


def _check_lines(store, branch, rows, exclude=None):
    """[(variant, qty, price, serials)] → refused when a variant is not this shop's, a serial is wrong / not on the
    shelf here / held already, or (when the shop blocks overselling) there is not enough free stock."""
    from inventory import serials as serial_engine
    from .checkout import held_qty, held_serials
    allow_negative = getattr(getattr(store, 'settings', None), 'allow_negative_stock', False)
    held = held_serials(store, exclude=exclude)
    for variant, qty, _price, serials in rows:
        name = variant.product.name
        if serials:
            if not serial_engine.is_tracked(variant):
                raise _err('serials_not_tracked', f'{name} does not track serial numbers.', product=name)
            if len(serials) > qty:
                raise _err('too_many_serials', f'More serials than units for {name}.', product=name)
            for sn in serials:
                if sn in held:
                    raise _err('serial_reserved', f'Serial {sn} is already held by reservation {held[sn]}.',
                               serial=sn, reservation=held[sn])
            serial_engine._in_stock_units(store, variant, branch, serials, name)
        if serial_engine.is_tracked(variant) and qty != qty.to_integral_value():
            raise _err('whole_units', f'{name} is sold in whole units.', product=name)
        if allow_negative or variant.product.product_type == variant.product.ProductType.SERVICE:
            continue
        stock = StockLevel.objects.select_for_update().filter(variant=variant, branch=branch).first()
        free = (stock.quantity if stock else ZERO) - held_qty(variant, branch, exclude=exclude)
        if qty > free:
            raise _err('not_enough_stock', f'Only {free.normalize()} of {name} free to hold here.',
                       product=name, available=free)


@transaction.atomic
def create_reservation(store, user, *, branch, customer, items, hold_until=None, note='', salesperson=None,
                       deposit_amount=None, deposit_method=None):
    """items = [{'variant': id, 'quantity', 'unit_price', 'serial_numbers': [...]}]. An optional first deposit."""
    from inventory.serials import clean_list
    if not is_on(store):
        raise _err('reservations_off', 'Reservations are turned off for this shop.')
    if branch is None or branch.store_id != store.id:
        raise _err('branch_required', 'Choose the branch the items are held at.')
    if customer is None or customer.store_id != store.id or customer.is_deleted or customer.is_walk_in:
        raise _err('customer_required', 'A reservation is for a named customer — choose one.')
    if not isinstance(items, list) or not items:
        raise _err('items_required', 'Add at least one item.')
    rows = []
    for raw in items:
        if not isinstance(raw, dict):
            raise _err('items_required', 'Each line needs an item and a quantity.')
        variant = ProductVariant.objects.filter(pk=raw.get('variant'), product__store=store).select_related(
            'product').first() if raw.get('variant') else None
        if variant is None:
            raise _err('variant_not_found', 'Item not found in this shop.')
        qty = _qty(raw.get('quantity'))
        try:
            price = Decimal(str(raw.get('unit_price'))).quantize(CENT)
        except (InvalidOperation, TypeError, ValueError):
            raise _err('bad_price', 'Write the price.')
        if not price.is_finite() or price < 0:
            raise _err('bad_price', 'The price can not be below zero.')
        serials = clean_list(raw.get('serial_numbers') or [], variant.product.name)
        rows.append((variant, qty, price, serials))
    _check_lines(store, branch, rows)

    seq, _ = ReservationSequence.objects.select_for_update().get_or_create(store=store)
    seq.last_number += 1
    seq.save(update_fields=['last_number'])
    res = Reservation.objects.create(
        store=store, branch=branch, customer=customer, number=seq.last_number, hold_until=hold_until,
        note=note or '', created_by=user, salesperson=salesperson or user,
        salesperson_name=person_name(salesperson or user),
        total=_price_total(store, [(v, q, p) for v, q, p, _s in rows]))
    for variant, qty, price, serials in rows:
        ReservationItem.objects.create(reservation=res, variant=variant, quantity=qty, unit_price=price,
                                       serial_numbers=serials)
    if deposit_amount not in (None, '', 0, '0'):
        take_deposit(res, user, amount=deposit_amount, method=deposit_method)
    return res


@transaction.atomic
def update_reservation(res, user, *, hold_until=..., note=None):
    """Only the hold-until date and the note change; the lines are the deal (cancel + make a new one to change them)."""
    res = _lock(res)
    _must_be_open(res)
    fields = ['updated_at']
    if hold_until is not ...:
        res.hold_until = hold_until
        fields.append('hold_until')
    if note is not None:
        res.note = note
        fields.append('note')
    res.save(update_fields=fields)
    return res


# ── deposits ───────────────────────────────────────────────────────────────────────────────────────────────────────
@transaction.atomic
def take_deposit(res, user, *, amount, method, note=''):
    res = _lock(res)
    _must_be_open(res)
    amount = _money(amount)
    method = _deposit_method(res.store, method)
    return _record(res, user, amount, method, note)


@transaction.atomic
def give_back(res, user, *, amount, method, box=None, note=''):
    """Part of the deposit handed back while the reservation stays open (e.g. the customer took fewer items)."""
    res = _lock(res)
    _must_be_open(res)
    return _give_back(res, user, _money(amount), method, box, note)


def _give_back(res, user, amount, method, box, note):
    method = _deposit_method(res.store, method)
    held = res.deposit_balance()
    if amount > held:
        raise _err('more_than_deposit', f'Only {held} is held as deposit.', held=held)
    return _record(res, user, -amount, method, note or 'Given back', box=box)


# ── convert ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _deposit_by_method(res):
    """[(method, net amount > 0)] in the order the deposits were taken."""
    order, net = [], {}
    for dep in res.deposits.select_related('method').order_by('created_at'):
        if dep.method_id not in net:
            order.append(dep.method)
            net[dep.method_id] = ZERO
        net[dep.method_id] += dep.amount
    return [(m, net[m.pk]) for m in order if net[m.pk] > 0]


@transaction.atomic
def convert(res, user, data, idem_key=''):
    """The reservation becomes a POSTED sale: its lines at the agreed prices, the deposit as the first payments, the
    rest paid now through the till's own checkout (`data` = the till's {"payments": [...]}) — every till rule applies.
    A deposit beyond the sale's total goes to the customer's store credit. Returns the sale."""
    from .checkout import post_sale
    res = _lock(res)
    if (res.status == Reservation.Status.CONVERTED and res.sale_id and idem_key
            and res.sale.checkout_key == idem_key):
        return res.sale                               # the same conversion sent twice (double click / retry)
    _must_be_open(res)
    store = res.store
    invoice = SalesInvoice.objects.create(store=store, branch=res.branch, customer=res.customer,
                                          status=SalesInvoice.Status.DRAFT, date=timezone.now(),
                                          salesperson=res.salesperson, salesperson_name=res.salesperson_name)
    # Serials picked now (a reservation of "a laptop" names the exact unit only when it is sold):
    # data["serials"] = {reservation item id: [serial, …]} — added to the line's held ones. The till's serial engine
    # checks the count and that each unit is on the shelf; checkout refuses one held by another reservation.
    from inventory.serials import clean_list
    picked = (data or {}).get('serials') or {}
    if not isinstance(picked, dict):
        raise _err('bad_serials', 'Serials must be given per line.')
    for item in res.items.select_related('variant__product'):
        serials = list(item.serial_numbers or [])
        extra = clean_list(picked.get(str(item.id)) or [], item.variant.product.name)
        serials += [sn for sn in extra if sn not in serials]
        SalesInvoiceItem.objects.create(invoice=invoice, variant=item.variant, quantity=item.quantity,
                                        unit_price=item.unit_price, serial_numbers=serials)
    from .serializers import SalesInvoiceSerializer
    SalesInvoiceSerializer._recalculate(invoice)
    invoice.refresh_from_db()
    res.sale = invoice
    res.save(update_fields=['sale', 'updated_at'])

    left = invoice.grand_total
    for method, amount in _deposit_by_method(res):
        part = min(amount, left)
        if part > 0:
            Payment.objects.create(invoice=invoice, method=method, amount=part, created_by=user, deposit_of=res)
            left -= part
    extra = res.deposit_balance() - (invoice.grand_total - left)
    invoice.refresh_from_db()
    if extra > 0:
        cust = Customer.all_objects.select_for_update().get(pk=res.customer_id)
        cust.store_credit = (cust.store_credit or ZERO) + extra
        cust.save(update_fields=['store_credit'])

    if left <= 0 and not (data or {}).get('payments'):
        data = {}                                      # fully paid by the deposit: nothing more to take
    post_sale(invoice, user, data or {}, idem_key)
    invoice.refresh_from_db()
    res.status = Reservation.Status.CONVERTED
    res.converted_at = timezone.now()
    res.converted_extra_to_credit = max(extra, ZERO)
    res.save(update_fields=['status', 'converted_at', 'converted_extra_to_credit', 'updated_at'])
    return invoice


# ── cancel ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@transaction.atomic
def cancel(res, user, *, outcome, reason, method=None, box=None):
    """Held stock is free again. The deposit left (if any): REFUNDED (money out of a box / drawer), KEPT (other income
    in the P&L on this date — the money stays where it is), TO_CREDIT (the customer's store credit)."""
    if not (reason or '').strip():
        raise _err('reason_required', 'Write why the reservation is cancelled.')
    res = _lock(res)
    _must_be_open(res)
    held = res.deposit_balance()
    O = Reservation.DepositOutcome
    if held <= 0:
        outcome = O.NONE
    elif outcome == O.REFUNDED:
        if method is None:
            last = res.deposits.filter(amount__gt=0).select_related('method').order_by('-created_at').first()
            method = last.method if last else None
        _give_back(res, user, held, method, box, 'Reservation cancelled')
    elif outcome == O.KEPT:
        res.kept_amount = held
    elif outcome == O.TO_CREDIT:
        cust = Customer.all_objects.select_for_update().get(pk=res.customer_id)
        cust.store_credit = (cust.store_credit or ZERO) + held
        cust.save(update_fields=['store_credit'])
    else:
        raise _err('outcome_required', 'Choose what happens to the deposit: give it back, keep it, or store credit.')
    res.status = Reservation.Status.CANCELLED
    res.deposit_outcome = outcome
    res.cancel_reason = reason.strip()
    res.cancelled_at = timezone.now()
    res.cancelled_by = user
    res.save(update_fields=['status', 'deposit_outcome', 'cancel_reason', 'cancelled_at', 'cancelled_by',
                            'kept_amount', 'updated_at'])
    return res


def kept_deposits(store, date_from, date_to):
    """Σ deposits kept by the shop on cancelled reservations in [date_from, date_to] — "other income" in the P&L."""
    return (Reservation.all_objects.filter(store=store, is_deleted=False, status=Reservation.Status.CANCELLED,
                                           deposit_outcome=Reservation.DepositOutcome.KEPT,
                                           cancelled_at__date__gte=date_from, cancelled_at__date__lte=date_to)
            .aggregate(s=Sum('kept_amount'))['s'] or ZERO)
