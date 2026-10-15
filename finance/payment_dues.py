"""Payment due dates (s173, PAYMENT-DUES.md) — the ONE door for every change, each atomic.

A due date is a REMINDER, never money: it changes no balance, no box, no shift. The debt is the customer's balance
(sales on credit, opening balance); the money comes in through the normal customer receipt (finance/receipts.py)
and "paid" only links that receipt. OPEN ──done──▶ DONE · OPEN ──cancel──▶ CANCELLED.

The reminder needs no scheduler: notify_due() runs when the notification bell refreshes and sends each due date
that reached its day ONCE (to the person who wrote it and the shop owner — never the whole shop: debts are a
manager's business).
"""
import calendar
import uuid
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import CustomerReceipt, PaymentDue, SalesInvoice

CENT = Decimal('0.01')
UNITS = ('DAY', 'WEEK', 'MONTH')
MAX_INSTALMENTS = 60
SOON_DAYS = 7
NOTIFY_ONE_BY_ONE = 5          # more due dates reached at once → one summary notification instead


def _err(code, detail, **extra):
    return ValidationError({'code': code, 'detail': detail, **{k: str(v) for k, v in extra.items()}})


def is_on(store):
    return bool(getattr(getattr(store, 'settings', None), 'payment_dues_enabled', False))


def _money(raw):
    try:
        val = Decimal(str(raw)).quantize(CENT)
    except (InvalidOperation, TypeError, ValueError):
        raise _err('bad_amount', 'Write the amount.')
    if not val.is_finite() or val <= 0:
        raise _err('bad_amount', 'The amount must be above zero.')
    return val


def add_months(d, n):
    """The same day n months later — the 31st becomes the month's last day when that month is shorter."""
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def schedule(first, count, every, unit):
    """The dates of `count` instalments, `every` DAY / WEEK / MONTH apart, starting on `first`."""
    if unit == 'MONTH':
        return [add_months(first, i * every) for i in range(count)]
    step = timedelta(days=every * (7 if unit == 'WEEK' else 1))
    return [first + i * step for i in range(count)]


def split(amount, count):
    """Equal parts in whole cents; the last part takes what is left so the parts add up exactly."""
    part = (amount / count).quantize(CENT, rounding=ROUND_DOWN)
    return [part] * (count - 1) + [amount - part * (count - 1)]


def _check_customer(store, customer):
    if customer is None or customer.store_id != store.id or customer.is_deleted or customer.is_walk_in:
        raise _err('customer_required', 'Choose the customer who will pay.')


@transaction.atomic
def create_dues(store, user, *, customer, amount, first_date, count=1, every=1, unit='MONTH', note='', invoice=None):
    """One due date (count=1) or a set of instalments that share a plan id. → list of PaymentDue."""
    if not is_on(store):
        raise _err('dues_off', 'Payment due dates are switched off for this shop (Settings → Capabilities).')
    _check_customer(store, customer)
    amount = _money(amount)
    if first_date is None:
        raise _err('due_required', 'Write the date.')
    try:
        count = 1 if count in (None, '') else int(count)
        every = 1 if every in (None, '') else int(every)
    except (TypeError, ValueError):
        raise _err('bad_plan', 'Write how many payments and how far apart.')
    if not 1 <= count <= MAX_INSTALMENTS:
        raise _err('bad_count', f'Between 1 and {MAX_INSTALMENTS} payments.')
    if unit not in UNITS or not 1 <= every <= 365:
        raise _err('bad_every', 'Choose how far apart the payments are.')
    if amount < CENT * count:
        raise _err('bad_amount', 'The amount is too small to split into that many payments.')
    if invoice is not None and (invoice.store_id != store.id or invoice.customer_id != customer.id
                                or invoice.is_deleted or invoice.status != SalesInvoice.Status.POSTED):
        raise _err('invoice_not_found', "That sale isn't this customer's.")
    plan = uuid.uuid4() if count > 1 else None
    return [PaymentDue.objects.create(
                store=store, customer=customer, amount=part, due_date=day, plan=plan,
                plan_seq=(i + 1) if plan else None, plan_count=count if plan else None,
                invoice=invoice, note=(note or '').strip(), created_by=user)
            for i, (day, part) in enumerate(zip(schedule(first_date, count, every, unit), split(amount, count)))]


def _lock(due):
    due = PaymentDue.all_objects.select_for_update().get(pk=due.pk)
    if due.status != PaymentDue.Status.OPEN or due.is_deleted:
        raise _err('not_open', 'This due date is already closed.')
    return due


@transaction.atomic
def update_due(due, user, *, due_date=None, amount=None, note=None):
    """Move the date / change the amount or the note — only while it is open."""
    due = _lock(due)
    fields = []
    if due_date is not None:
        due.due_date, due.notified_on = due_date, None      # a new day gets its own reminder
        fields += ['due_date', 'notified_on']
    if amount is not None:
        due.amount = _money(amount)
        fields.append('amount')
    if note is not None:
        due.note = note.strip()
        fields.append('note')
    if fields:
        due.save(update_fields=fields + ['updated_at'])
    return due


@transaction.atomic
def mark_done(due, user, *, receipt=None, reason=''):
    """Paid (link the customer receipt that brought the money) — or closed without one, with a reason."""
    due = _lock(due)
    if receipt is not None:
        if receipt.store_id != due.store_id or receipt.customer_id != due.customer_id or receipt.is_deleted:
            raise _err('receipt_not_found', "That payment isn't this customer's.")
    elif not (reason or '').strip():
        raise _err('reason_required', 'Take the payment, or write why it is closed without one.')
    due.status, due.receipt, due.done_on = PaymentDue.Status.DONE, receipt, timezone.localdate()
    due.status_reason = (reason or '').strip()
    due.save(update_fields=['status', 'receipt', 'done_on', 'status_reason', 'updated_at'])
    return due


@transaction.atomic
def cancel_due(due, user, reason):
    if not (reason or '').strip():
        raise _err('reason_required', 'Write why it is cancelled.')
    due = _lock(due)
    due.status, due.status_reason = PaymentDue.Status.CANCELLED, reason.strip()
    due.save(update_fields=['status', 'status_reason', 'updated_at'])
    return due


def summary(store):
    """Open due dates that are late / due today / due in the next 7 days: counts + totals."""
    from django.db.models import Count, Sum
    today = timezone.localdate()
    base = PaymentDue.objects.filter(store=store, status=PaymentDue.Status.OPEN)
    out = {}
    for key, qs in (('late', base.filter(due_date__lt=today)), ('today', base.filter(due_date=today)),
                    ('soon', base.filter(due_date__gt=today, due_date__lte=today + timedelta(days=SOON_DAYS)))):
        agg = qs.aggregate(n=Count('id'), total=Sum('amount'))
        out[key] = {'count': agg['n'] or 0, 'total': str(agg['total'] or Decimal('0'))}
    return out


def notify_due(store):
    """Send each open due date that reached its day ONCE — to whoever wrote it and the shop owner."""
    from core.models import StoreSettings
    # read the switch fresh (the bell polls this on every refresh; a cached store.settings could be stale)
    if store is None or not StoreSettings.objects.filter(store_id=store.id, payment_dues_enabled=True).exists():
        return 0
    from notifications.dispatcher import msg, send_notification
    from notifications.models import Notification
    today = timezone.localdate()
    with transaction.atomic():
        dues = list(PaymentDue.objects.select_for_update(skip_locked=True, of=('self',))
                    .filter(store=store, status=PaymentDue.Status.OPEN, due_date__lte=today, notified_on__isnull=True)
                    .select_related('customer', 'created_by'))
        if not dues:
            return 0
        readers = {}
        for d in dues:
            for u in (d.created_by, store.owner):
                if u is not None and u.is_active:
                    readers.setdefault(u.pk, (u, []))[1].append(d)
        for u, mine in readers.values():
            if len(mine) > NOTIFY_ONE_BY_ONE:
                total = sum((d.amount for d in mine), Decimal('0'))
                send_notification(store, f'{len(mine)} payment dates reached', f'Total {total}',
                                  priority=Notification.Priority.WARNING, notif_type=Notification.Type.PAYMENT_DUE,
                                  user=u, link='/finance/due-dates',
                                  i18n=msg('payment_due.many_title', 'payment_due.many_body', count=len(mine), total=total))
                continue
            for d in mine:
                late = d.due_date < today
                send_notification(store, f'Payment due: {d.customer.name}', f'{d.amount} on {d.due_date}',
                                  priority=Notification.Priority.WARNING, notif_type=Notification.Type.PAYMENT_DUE,
                                  user=u, link='/finance/due-dates',
                                  i18n=msg('payment_due.late_title' if late else 'payment_due.title', 'payment_due.body',
                                           customer=d.customer.name, amount=d.amount, date=d.due_date.isoformat()))
        PaymentDue.all_objects.filter(pk__in=[d.pk for d in dues]).update(notified_on=today)
    return len(dues)
