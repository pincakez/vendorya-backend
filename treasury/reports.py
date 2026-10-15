"""Treasury Wave 3 reports (TREASURY.md § 8, PROLINE-PARITY New 9) — read-only maths over the immutable lines.

Shop (managers): box balances on a date · movements by kind / box / day. Owner (owner level): the same over every box of
every part they own, plus owner draws (+ per-day average), money held for others (amanat) and partners.

One rule for reversals: a REVERSAL line counts under the kind of the line it cancels (a reversed sale payment lowers
"Sale payment", it is not its own kind), and a reversed line is never counted as an event.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Case, CharField, Count, F, Q, Sum, When
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Amana, MoneyMove, PartnerSettlementLine, Partner

ZERO = Decimal('0')
K = MoneyMove.Kind


def money(v):
    return str((v or ZERO).quantize(Decimal('0.01')))


def day_start(d):
    return timezone.make_aware(datetime.combine(d, time.min), timezone.get_current_timezone())


def _range(qs, df, dt):
    if df:
        qs = qs.filter(at__gte=day_start(df))
    if dt:
        qs = qs.filter(at__lt=day_start(dt + timedelta(days=1)))
    return qs


def _effective_kind(qs):
    return qs.annotate(eff_kind=Case(When(kind=K.REVERSAL, then=F('reverses__kind')), default=F('kind'),
                                     output_field=CharField()))


# ── balances on a date ───────────────────────────────────────────────────────────────────────────────────────────
def balances_on(boxes, on_date):
    """Every box's balance at the END of `on_date` (local day) — closed boxes only when they held money then.
    → {date, boxes: [{id, name, kind, part, part_name, is_active, balance}], by_kind: {kind: total}, total}"""
    end = day_start(on_date + timedelta(days=1))
    sums = dict(MoneyMove.all_objects.filter(box__in=boxes, at__lt=end).values('box_id')
                .annotate(s=Sum('amount')).values_list('box_id', 's'))
    rows, by_kind, total = [], defaultdict(lambda: ZERO), ZERO
    for b in boxes.select_related('part').order_by('part__kind', 'part__name', 'kind', 'name'):
        bal = sums.get(b.pk) or ZERO
        if not b.is_active and bal == 0:
            continue
        if b.created_at >= end and bal == 0:
            continue                                   # did not exist yet on that day
        rows.append({'id': str(b.pk), 'name': b.name, 'kind': b.kind, 'system_role': b.system_role,
                     'part': str(b.part_id), 'part_kind': b.part.kind, 'part_name': b.part.name,
                     'is_active': b.is_active, 'balance': money(bal)})
        by_kind[b.kind] += bal
        total += bal
    return {'date': on_date.isoformat(), 'boxes': rows,
            'by_kind': {k: money(v) for k, v in by_kind.items()}, 'total': money(total)}


# ── movements ────────────────────────────────────────────────────────────────────────────────────────────────────
def movements(lines, df, dt):
    """Money in / out in [df, dt] over `lines` (a MoneyMove queryset already scoped to what the caller may see).
    → {by_kind: [{kind, money_in, money_out, net, count}], by_box: [...], by_day: [{day, money_in, money_out}], totals}"""
    lines = _effective_kind(_range(lines, df, dt))
    pos, neg = Q(amount__gt=0), Q(amount__lt=0)
    real = Q(reversed_by__isnull=True) & ~Q(kind=K.REVERSAL)   # events that still stand (for the count)

    def agg(qs):
        return qs.annotate(money_in=Sum('amount', filter=pos), money_out=Sum('amount', filter=neg),
                           count=Count('id', filter=real))

    def shape(r, **keys):
        i, o = r['money_in'] or ZERO, -(r['money_out'] or ZERO)
        return {**keys, 'money_in': money(i), 'money_out': money(o), 'net': money(i - o), 'count': r['count']}

    by_kind = [shape(r, kind=r['eff_kind']) for r in agg(lines.values('eff_kind')).order_by('eff_kind')]
    by_box = [shape(r, box=str(r['box_id']), name=r['box__name'], box_kind=r['box__kind'])
              for r in agg(lines.values('box_id', 'box__name', 'box__kind')).order_by('box__name')]
    by_day = [shape(r, day=r['day'].isoformat())
              for r in agg(lines.annotate(day=TruncDate('at')).values('day')).order_by('day')]
    tot = lines.aggregate(i=Sum('amount', filter=pos), o=Sum('amount', filter=neg))
    i, o = tot['i'] or ZERO, -(tot['o'] or ZERO)
    # Transfers inside the scope move money between the caller's own boxes — shown, but not "money in / out".
    inner = {K.TRANSFER_IN, K.TRANSFER_OUT}
    ti = sum((Decimal(r['money_in']) for r in by_kind if r['kind'] in inner), ZERO)
    to = sum((Decimal(r['money_out']) for r in by_kind if r['kind'] in inner), ZERO)
    return {'date_from': df.isoformat() if df else None, 'date_to': dt.isoformat() if dt else None,
            'by_kind': by_kind, 'by_box': by_box, 'by_day': by_day,
            'totals': {'money_in': money(i), 'money_out': money(o), 'net': money(i - o),
                       'money_in_without_transfers': money(i - ti), 'money_out_without_transfers': money(o - to)}}


# ── owner draws ──────────────────────────────────────────────────────────────────────────────────────────────────
def draws(owner_lines, df, dt):
    """Money the owner took OUT of their businesses (DRAW_OUT) and put back IN (PUT_IN), per business part and month,
    with the per-day average over the range (inclusive days). Reversed lines are left out (they never happened)."""
    live = _range(owner_lines, df, dt).filter(reversed_by__isnull=True)
    out = live.filter(kind=K.DRAW_OUT)
    put = live.filter(kind=K.PUT_IN)
    parts = defaultdict(lambda: {'drawn': ZERO, 'put_in': ZERO, 'name': '', 'kind': ''})
    for qs, key, sign in ((out, 'drawn', -1), (put, 'put_in', 1)):
        for r in qs.values('part_id', 'part__name', 'part__kind').annotate(s=Sum('amount')):
            p = parts[r['part_id']]
            p[key] += sign * (r['s'] or ZERO)
            p['name'], p['kind'] = r['part__name'], r['part__kind']
    months = defaultdict(lambda: ZERO)
    for at, amount in out.values_list('at', 'amount'):
        months[timezone.localtime(at).strftime('%Y-%m')] += -amount
    drawn = sum((p['drawn'] for p in parts.values()), ZERO)
    put_in = sum((p['put_in'] for p in parts.values()), ZERO)
    first = df
    if first is None:
        earliest = out.order_by('at').values_list('at', flat=True).first()
        first = timezone.localtime(earliest).date() if earliest else None
    last = dt or timezone.localdate()
    days = (last - first).days + 1 if first and last >= first else 0
    return {'date_from': df.isoformat() if df else None, 'date_to': dt.isoformat() if dt else None, 'days': days,
            'drawn': money(drawn), 'put_in': money(put_in), 'net_drawn': money(drawn - put_in),
            'per_day': money(drawn / days if days else ZERO),
            'by_part': [{'part': str(pid), 'name': p['name'], 'part_kind': p['kind'], 'drawn': money(p['drawn']),
                         'put_in': money(p['put_in']), 'net_drawn': money(p['drawn'] - p['put_in'])}
                        for pid, p in sorted(parts.items(), key=lambda kv: kv[1]['name'])],
            'by_month': [{'month': m, 'drawn': money(v)} for m, v in sorted(months.items())]}


# ── amanat ───────────────────────────────────────────────────────────────────────────────────────────────────────
def amanat(owner, on_date, df=None, dt=None):
    """Money held for other people: each one's balance at the end of `on_date`, plus what came in / went back in
    [df, dt] when given. Closed ones only when they still held money then."""
    end = day_start(on_date + timedelta(days=1))
    rows, total = [], ZERO
    for a in Amana.objects.filter(owner=owner).select_related('part').order_by('held_for'):
        lines = MoneyMove.all_objects.filter(amana=a)
        held = lines.filter(at__lt=end).aggregate(s=Sum('amount'))['s'] or ZERO
        rng = _range(lines, df, dt) if (df or dt) else lines.none()
        got = rng.filter(amount__gt=0).aggregate(s=Sum('amount'))['s'] or ZERO
        back = -(rng.filter(amount__lt=0).aggregate(s=Sum('amount'))['s'] or ZERO)
        if held == 0 and a.is_closed and not (got or back):
            continue
        rows.append({'id': str(a.pk), 'held_for': a.held_for, 'part_name': a.part.name, 'part_kind': a.part.kind,
                     'opened_on': a.opened_on.isoformat(), 'is_closed': a.is_closed, 'held': money(held),
                     'received': money(got), 'returned': money(back)})
        total += held
    return {'date': on_date.isoformat(), 'amanat': rows, 'total': money(total)}


# ── partners ─────────────────────────────────────────────────────────────────────────────────────────────────────
def partners(owner, df, dt):
    """Per partner: share, profit share settled in [df, dt] (settlements whose period ENDS in the range, not
    cancelled), money put in / paid out in the range, and what the business owes them now (+) or they owe it (−)."""
    from . import ledger
    rows = []
    for p in Partner.objects.filter(owner=owner).select_related('part').order_by('part__name', 'name'):
        sl = PartnerSettlementLine.objects.filter(partner=p, settlement__cancelled_at__isnull=True)
        if df:
            sl = sl.filter(settlement__date_to__gte=df)
        if dt:
            sl = sl.filter(settlement__date_to__lte=dt)
        moves = _range(MoneyMove.all_objects.filter(partner=p), df, dt)
        rows.append({'id': str(p.pk), 'name': p.name, 'part_name': p.part.name, 'share_pct': str(p.share_pct),
                     'is_active': p.is_active,
                     'settled': money(sl.aggregate(s=Sum('amount'))['s']),
                     'settlements': sl.count(),
                     'put_in': money(moves.filter(amount__gt=0).aggregate(s=Sum('amount'))['s']),
                     'paid': money(-(moves.filter(amount__lt=0).aggregate(s=Sum('amount'))['s'] or ZERO)),
                     'balance_now': money(ledger.partner_account(p))})
    return {'date_from': df.isoformat() if df else None, 'date_to': dt.isoformat() if dt else None, 'partners': rows}
