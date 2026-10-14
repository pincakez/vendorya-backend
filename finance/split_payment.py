"""Split payment at the till (s167, PROLINE-PARITY Upgrades 1 + 5).

The checkout call carries the WHOLE payment — one step, never two calls (s156). The till sends

    payments: [{"method": <PaymentMethod id> | "STORE_CREDIT", "amount": "600.00"}, ...]

and plan_checkout_payments() turns that into what to store, before anything is written:

* Lines with the same method are MERGED (two cash lines = one cash Payment).
* Zero / negative / non-numeric amounts are refused (400). An Ajel line's amount is ignored: Ajel is not money
  received, it means "leave whatever is still unpaid on the customer's account".
* Card / bank / store-credit lines together may not be more than what is due (400). Only CASH may overpay —
  the extra is CHANGE: it is handed back, the cash Payment stores only the money kept, and the sale's
  change_given remembers it for the receipt.
* Whatever is still unpaid needs an explicit Ajel line (400 otherwise), so a cashier can't put a sale on
  credit by mistake. Ajel needs a named customer (never Walk-in) and is judged by the shop's credit policy
  in checkout, exactly as before, with the real paid total.
* "STORE_CREDIT" spends the customer's wallet (Customer.store_credit): named customer only, never more than
  they have. checkout() re-checks + deducts it with the customer row LOCKED, inside the same atomic block
  that posts the sale, and records it as a Payment on the shop's hidden system "Store credit" method — so
  the sale's paid_amount, the receipt, the payments log and a later return all see it like any other tender,
  and it never counts as drawer cash (is_cash = False).

The old single `method` field still works (no `payments`): a normal method pays the whole balance, an Ajel
method leaves it all unpaid, no method posts unpaid.
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError

from .models import PaymentMethod

ZERO = Decimal('0')
CENT = Decimal('0.01')
STORE_CREDIT = 'STORE_CREDIT'


def _bad(msg):
    raise ValidationError({'detail': msg})


def store_credit_method(store):
    """The shop's one system "Store credit" method — made on first use, revived if it was ever deleted."""
    m = PaymentMethod.all_objects.filter(store=store, is_store_credit=True).first()
    if m is None:
        try:
            with transaction.atomic():
                m = PaymentMethod.all_objects.create(store=store, name='Store credit', is_store_credit=True)
        except IntegrityError:   # another checkout made it a moment ago
            m = PaymentMethod.all_objects.get(store=store, is_store_credit=True)
    if m.is_deleted:
        m.restore()
    return m


@dataclass
class PaymentPlan:
    lines: list = field(default_factory=list)   # [(PaymentMethod, amount kept)] — what becomes Payment rows
    change: Decimal = ZERO                       # cash handed back
    ajel: bool = False                           # the rest (if any) goes on the customer's account
    store_credit: Decimal = ZERO                 # wallet money spent (also one of `lines`)

    @property
    def paying(self):
        return sum((a for _, a in self.lines), ZERO)


def _amount(raw):
    try:
        a = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        _bad('Each payment line needs an amount.')
    if not a.is_finite():
        _bad('Each payment line needs an amount.')
    if a <= 0:
        _bad('A payment line must be more than zero.')
    if a != a.quantize(CENT):
        _bad('Amounts can have at most 2 decimals.')
    return a


def _method(store, mid):
    try:   # a malformed id must be a clean 400, not a 500
        m = PaymentMethod.objects.filter(store=store, pk=mid).first() if mid else None
    except (ValueError, TypeError, DjangoValidationError):
        m = None
    if m is None:
        _bad('Unknown payment method.')
    return m


def _is_named(customer):
    return customer is not None and not customer.is_walk_in


def plan_checkout_payments(invoice, data):
    """Validate the till's payment for a DRAFT `invoice`; nothing is written. Raises 400 on any problem."""
    store, customer = invoice.store, invoice.customer
    remaining = max((invoice.grand_total or ZERO) - (invoice.paid_amount or ZERO), ZERO)
    raw = data.get('payments')

    if raw is None:                                   # ── old single-method request (kept as it was)
        if not data.get('method'):
            return PaymentPlan()                      # post unpaid — callers that pay separately
        m = _method(store, data['method'])
        if m.is_store_credit:
            _bad('Use a STORE_CREDIT payment line to spend store credit.')
        if m.is_agel:
            if not _is_named(customer):
                _bad('A credit (Ajel) sale needs a named customer — pick one first.')
            return PaymentPlan(ajel=True)
        return PaymentPlan(lines=[(m, remaining)] if remaining > 0 else [])

    if not isinstance(raw, list) or not raw:
        _bad('Add at least one payment line.')

    merged, ajel = {}, False                          # key → [method, amount]; dict keeps the till's order
    for line in raw:
        if not isinstance(line, dict):
            _bad('Each payment line needs a method and an amount.')
        mid = line.get('method')
        if mid == STORE_CREDIT:
            m = None
            key = STORE_CREDIT
        else:
            m = _method(store, mid)
            if m.is_store_credit:
                key, m = STORE_CREDIT, None
            elif m.is_agel:
                ajel = True
                continue
            else:
                key = str(m.pk)
        amt = _amount(line.get('amount'))
        if key in merged:
            merged[key][1] += amt
        else:
            merged[key] = [m, amt]

    sc = merged[STORE_CREDIT][1] if STORE_CREDIT in merged else ZERO
    if sc > 0:
        if not _is_named(customer):
            _bad('Store credit needs a named customer — pick one first.')
        if sc > (customer.store_credit or ZERO):
            _bad(f'{customer.name} has only {(customer.store_credit or ZERO):.2f} store credit.')
        merged[STORE_CREDIT][0] = store_credit_method(store)
    if ajel and not _is_named(customer):
        _bad('A credit (Ajel) sale needs a named customer — pick one first.')

    lines = list(merged.values())
    cash_total = sum((a for m, a in lines if m.is_cash), ZERO)
    other_total = sum((a for m, a in lines if not m.is_cash), ZERO)
    if other_total > remaining:
        _bad(f'Card, bank and store-credit lines add up to more than is due ({remaining:.2f}). '
             f'Only cash can be more — the extra is given back as change.')
    change = max(cash_total + other_total - remaining, ZERO)   # ≤ cash_total, because other_total ≤ remaining

    # Take the change back out of the cash lines (first cash line first); a cash line that ends at 0 is dropped.
    kept, left = [], change
    for m, a in lines:
        if m.is_cash and left > 0:
            off = min(a, left)
            a, left = a - off, left - off
        if a > 0:
            kept.append((m, a))

    plan = PaymentPlan(lines=kept, change=change, ajel=ajel, store_credit=sc)
    if remaining - plan.paying > 0 and not ajel:
        _bad(f'{(remaining - plan.paying):.2f} is still to pay — add a payment line, '
             f"or put the rest on the customer's account (Ajel).")
    return plan
