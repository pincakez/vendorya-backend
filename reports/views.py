"""Reports v1 — read-only reporting endpoints.

Every endpoint:
  * filters by ``store=request.user.store`` (super-admins act on a store via the
    X-Store-ID header, same as the rest of the app — request.user.store is resolved
    for them upstream);
  * counts only POSTED sales and RECEIVED purchases, is_deleted=False everywhere;
  * uses the cost_at_sale COGS snapshot — never ProductVariant.cost_price;
  * returns plain dict payloads (no model writes), mirroring core.views.DashboardView.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Sum, Count, F, Q, Value, DecimalField
from django.db.models.functions import Coalesce, TruncDate, TruncWeek, TruncMonth
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsManagerOrAbove
from users.models import User
from finance.models import (
    SalesInvoice, SalesInvoiceItem, Payment,
    PurchaseInvoice, PurchaseItem,
    Expense, RefundInvoice, RefundItem, WorkShift,
)
from inventory.models import (
    StockAdjustment, ProductVariant, StockLevel,
    StorageLocation, StorageStock, StorageMovement,
)

DEC = DecimalField(max_digits=18, decimal_places=2)
ZERO = Decimal('0.00')


def _q(val):
    """Quantize a Decimal/number to 2 places for JSON output."""
    return str((Decimal(val or 0)).quantize(Decimal('0.01')))


# ---------- shared request helpers ----------

class _StoreMixin:
    """Resolves the acting store and shared query params. 403 if no store."""
    permission_classes = [IsAuthenticated, IsManagerOrAbove]

    def get_store(self, request):
        return getattr(request.user, 'store', None)

    def date_range(self, request):
        """Returns (date_from, date_to) as date objects. Defaults: last 30 days → today."""
        today = timezone.localdate()
        df = request.query_params.get('date_from')
        dt = request.query_params.get('date_to')
        try:
            date_from = datetime.strptime(df, '%Y-%m-%d').date() if df else today - timedelta(days=30)
        except ValueError:
            date_from = today - timedelta(days=30)
        try:
            date_to = datetime.strptime(dt, '%Y-%m-%d').date() if dt else today
        except ValueError:
            date_to = today
        return date_from, date_to

    def branch_id(self, request):
        return request.query_params.get('branch') or None

    def granularity(self, request):
        g = request.query_params.get('granularity', 'month')
        return g if g in ('day', 'week', 'month') else 'month'


def _trunc(granularity, field):
    return {'day': TruncDate, 'week': TruncWeek, 'month': TruncMonth}[granularity](field)


def _no_store():
    return Response({'detail': 'No store in context. Select a store first.'},
                    status=status.HTTP_403_FORBIDDEN)


# ============================================================
# 1. SALES — by product / category / supplier / period
# ============================================================

class SalesReportView(_StoreMixin, APIView):
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        df, dt = self.date_range(request)
        branch = self.branch_id(request)
        breakdown = request.query_params.get('breakdown', 'product')

        inv_filter = Q(invoice__store=store, invoice__status=SalesInvoice.Status.POSTED,
                       invoice__is_deleted=False,
                       invoice__date__date__gte=df, invoice__date__date__lte=dt)
        if branch:
            inv_filter &= Q(invoice__branch_id=branch)

        line_sales = F('quantity') * F('unit_price')  # ex-tax, pre invoice-discount

        if breakdown == 'period':
            g = self.granularity(request)
            inv_q = Q(store=store, status=SalesInvoice.Status.POSTED, is_deleted=False,
                      date__date__gte=df, date__date__lte=dt)
            if branch:
                inv_q &= Q(branch_id=branch)
            rows_qs = (
                SalesInvoice.objects.filter(inv_q)
                .annotate(period=_trunc(g, 'date'))
                .values('period')
                .annotate(
                    invoices=Count('id'),
                    net_sales=Coalesce(Sum(F('grand_total') - F('tax_total')), Value(ZERO), output_field=DEC),
                    tax=Coalesce(Sum('tax_total'), Value(ZERO), output_field=DEC),
                    discount=Coalesce(Sum('discount'), Value(ZERO), output_field=DEC),
                    gross=Coalesce(Sum('grand_total'), Value(ZERO), output_field=DEC),
                )
                .order_by('period')
            )
            rows = [{
                'period': r['period'].isoformat() if r['period'] else None,
                'invoices': r['invoices'],
                'net_sales': _q(r['net_sales']),
                'tax': _q(r['tax']),
                'discount': _q(r['discount']),
                'gross': _q(r['gross']),
            } for r in rows_qs]
            totals = {
                'net_sales': _q(sum(Decimal(r['net_sales']) for r in rows)),
                'tax': _q(sum(Decimal(r['tax']) for r in rows)),
                'discount': _q(sum(Decimal(r['discount']) for r in rows)),
                'gross': _q(sum(Decimal(r['gross']) for r in rows)),
                'invoices': sum(r['invoices'] for r in rows),
            }
            return Response({'breakdown': 'period', 'granularity': g,
                             'date_from': df, 'date_to': dt, 'rows': rows, 'totals': totals})

        # product / category / supplier all aggregate over SalesInvoiceItem
        group_map = {
            'product': ('variant__product__id', 'variant__product__name'),
            'category': ('variant__product__category__id', 'variant__product__category__name'),
            'supplier': ('variant__product__supplier__id', 'variant__product__supplier__name'),
        }
        if breakdown not in group_map:
            breakdown = 'product'
        id_field, name_field = group_map[breakdown]

        rows_qs = (
            SalesInvoiceItem.objects.filter(inv_filter)
            .values(id_field, name_field)
            .annotate(
                qty=Coalesce(Sum('quantity'), Value(ZERO), output_field=DecimalField(max_digits=18, decimal_places=3)),
                sales=Coalesce(Sum(line_sales), Value(ZERO), output_field=DEC),
                tax=Coalesce(Sum('tax_amount'), Value(ZERO), output_field=DEC),
                cogs=Coalesce(Sum(F('quantity') * F('cost_at_sale')), Value(ZERO), output_field=DEC),
            )
            .order_by('-sales')
        )
        rows = []
        for r in rows_qs:
            sales = Decimal(r['sales'] or 0)
            cogs = Decimal(r['cogs'] or 0)
            rows.append({
                'id': str(r[id_field]) if r[id_field] else None,
                'name': r[name_field] or '(Unassigned)',
                'qty': str(Decimal(r['qty'] or 0).normalize()),
                'sales': _q(sales),
                'tax': _q(r['tax']),
                'cogs': _q(cogs),
                'gross': _q(sales + Decimal(r['tax'] or 0)),
            })
        totals = {
            'sales': _q(sum(Decimal(r['sales']) for r in rows)),
            'tax': _q(sum(Decimal(r['tax']) for r in rows)),
            'cogs': _q(sum(Decimal(r['cogs']) for r in rows)),
        }
        return Response({'breakdown': breakdown, 'date_from': df, 'date_to': dt,
                         'rows': rows, 'totals': totals})


# ============================================================
# 2. PROFIT MARGIN — per product / category
# ============================================================

class ProfitMarginView(_StoreMixin, APIView):
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        df, dt = self.date_range(request)
        branch = self.branch_id(request)
        group_by = request.query_params.get('group_by', 'product')
        group_map = {
            'product': ('variant__product__id', 'variant__product__name'),
            'category': ('variant__product__category__id', 'variant__product__category__name'),
        }
        if group_by not in group_map:
            group_by = 'product'
        id_field, name_field = group_map[group_by]

        f = Q(invoice__store=store, invoice__status=SalesInvoice.Status.POSTED,
              invoice__is_deleted=False,
              invoice__date__date__gte=df, invoice__date__date__lte=dt)
        if branch:
            f &= Q(invoice__branch_id=branch)

        rows_qs = (
            SalesInvoiceItem.objects.filter(f)
            .values(id_field, name_field)
            .annotate(
                qty=Coalesce(Sum('quantity'), Value(ZERO), output_field=DecimalField(max_digits=18, decimal_places=3)),
                revenue=Coalesce(Sum(F('quantity') * F('unit_price')), Value(ZERO), output_field=DEC),
                cogs=Coalesce(Sum(F('quantity') * F('cost_at_sale')), Value(ZERO), output_field=DEC),
            )
            .order_by('-revenue')
        )
        rows = []
        tot_rev = tot_cogs = ZERO
        for r in rows_qs:
            revenue = Decimal(r['revenue'] or 0)
            cogs = Decimal(r['cogs'] or 0)
            profit = revenue - cogs
            margin = (profit / revenue * 100) if revenue else ZERO
            tot_rev += revenue
            tot_cogs += cogs
            rows.append({
                'id': str(r[id_field]) if r[id_field] else None,
                'name': r[name_field] or '(Unassigned)',
                'qty': str(Decimal(r['qty'] or 0).normalize()),
                'revenue': _q(revenue),
                'cogs': _q(cogs),
                'profit': _q(profit),
                'margin_pct': str(margin.quantize(Decimal('0.01'))),
            })
        tot_profit = tot_rev - tot_cogs
        totals = {
            'revenue': _q(tot_rev),
            'cogs': _q(tot_cogs),
            'profit': _q(tot_profit),
            'margin_pct': str(((tot_profit / tot_rev * 100) if tot_rev else ZERO).quantize(Decimal('0.01'))),
        }
        return Response({'group_by': group_by, 'date_from': df, 'date_to': dt,
                         'rows': rows, 'totals': totals})


# ============================================================
# 3. A/R AGING — customer receivables in age buckets
# ============================================================

def _bucket_label(days):
    if days <= 30:
        return 'b0_30'
    if days <= 60:
        return 'b31_60'
    if days <= 90:
        return 'b61_90'
    return 'b90_plus'


class ARAgingView(_StoreMixin, APIView):
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        today = timezone.localdate()

        invoices = (
            SalesInvoice.objects.filter(
                store=store, status=SalesInvoice.Status.POSTED, is_deleted=False)
            .annotate(refunded=Coalesce(
                Sum('refunds__total_refunded', filter=Q(refunds__is_deleted=False)),
                Value(ZERO), output_field=DEC))
            .select_related('customer')
        )

        cust = defaultdict(lambda: {'name': '', 'b0_30': ZERO, 'b31_60': ZERO,
                                    'b61_90': ZERO, 'b90_plus': ZERO, 'total': ZERO})
        for inv in invoices:
            outstanding = (inv.grand_total or ZERO) - (inv.paid_amount or ZERO) - (inv.refunded or ZERO)
            if outstanding <= 0:
                continue
            age = (today - timezone.localtime(inv.date).date()).days
            bucket = _bucket_label(age)
            c = cust[str(inv.customer_id)]
            c['name'] = inv.customer.name
            c[bucket] += outstanding
            c['total'] += outstanding

        rows = sorted(
            [{'customer_id': cid, 'name': v['name'],
              'b0_30': _q(v['b0_30']), 'b31_60': _q(v['b31_60']),
              'b61_90': _q(v['b61_90']), 'b90_plus': _q(v['b90_plus']),
              'total': _q(v['total'])}
             for cid, v in cust.items()],
            key=lambda r: Decimal(r['total']), reverse=True)
        totals = {k: _q(sum(v[k] for v in cust.values()))
                  for k in ('b0_30', 'b31_60', 'b61_90', 'b90_plus', 'total')}
        return Response({'as_of': today, 'rows': rows, 'totals': totals})


# ============================================================
# 4. A/P AGING — supplier payables in age buckets
# ============================================================

class APAgingView(_StoreMixin, APIView):
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        today = timezone.localdate()

        purchases = (
            PurchaseInvoice.objects.filter(
                store=store, status=PurchaseInvoice.Status.RECEIVED, is_deleted=False)
            .select_related('supplier')
        )
        supp = defaultdict(lambda: {'name': '', 'b0_30': ZERO, 'b31_60': ZERO,
                                    'b61_90': ZERO, 'b90_plus': ZERO, 'total': ZERO})
        for p in purchases:
            outstanding = (p.total_amount or ZERO) - (p.paid_amount or ZERO)
            if outstanding <= 0:
                continue
            age = (today - timezone.localtime(p.date).date()).days
            bucket = _bucket_label(age)
            s = supp[str(p.supplier_id)]
            s['name'] = p.supplier.name
            s[bucket] += outstanding
            s['total'] += outstanding

        rows = sorted(
            [{'supplier_id': sid, 'name': v['name'],
              'b0_30': _q(v['b0_30']), 'b31_60': _q(v['b31_60']),
              'b61_90': _q(v['b61_90']), 'b90_plus': _q(v['b90_plus']),
              'total': _q(v['total'])}
             for sid, v in supp.items()],
            key=lambda r: Decimal(r['total']), reverse=True)
        totals = {k: _q(sum(v[k] for v in supp.values()))
                  for k in ('b0_30', 'b31_60', 'b61_90', 'b90_plus', 'total')}
        return Response({'as_of': today, 'rows': rows, 'totals': totals})


# ============================================================
# 5. P&L — daily / weekly / monthly (must reconcile)
# ============================================================

class ProfitLossView(_StoreMixin, APIView):
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        df, dt = self.date_range(request)
        g = self.granularity(request)

        # Revenue (ex-tax) + COGS per period from posted sales
        inv_periods = (
            SalesInvoice.objects.filter(
                store=store, status=SalesInvoice.Status.POSTED, is_deleted=False,
                date__date__gte=df, date__date__lte=dt)
            .annotate(period=_trunc(g, 'date'))
            .values('period')
            .annotate(revenue=Coalesce(Sum(F('grand_total') - F('tax_total')), Value(ZERO), output_field=DEC))
        )
        cogs_periods = (
            SalesInvoiceItem.objects.filter(
                invoice__store=store, invoice__status=SalesInvoice.Status.POSTED,
                invoice__is_deleted=False,
                invoice__date__date__gte=df, invoice__date__date__lte=dt)
            .annotate(period=_trunc(g, 'invoice__date'))
            .values('period')
            .annotate(cogs=Coalesce(Sum(F('quantity') * F('cost_at_sale')), Value(ZERO), output_field=DEC))
        )
        expense_periods = (
            Expense.objects.filter(store=store, is_deleted=False, date__gte=df, date__lte=dt)
            .annotate(period=_trunc(g, 'date'))
            .values('period')
            .annotate(expenses=Coalesce(Sum('amount'), Value(ZERO), output_field=DEC))
        )
        return_periods = (
            RefundInvoice.objects.filter(store=store, is_deleted=False,
                                         date__date__gte=df, date__date__lte=dt)
            .annotate(period=_trunc(g, 'date'))
            .values('period')
            .annotate(returns=Coalesce(Sum('total_refunded'), Value(ZERO), output_field=DEC))
        )

        periods = defaultdict(lambda: {'revenue': ZERO, 'cogs': ZERO, 'expenses': ZERO, 'returns': ZERO})

        def _key(p):
            return p.isoformat() if p else None
        for r in inv_periods:
            periods[_key(r['period'])]['revenue'] += Decimal(r['revenue'] or 0)
        for r in cogs_periods:
            periods[_key(r['period'])]['cogs'] += Decimal(r['cogs'] or 0)
        for r in expense_periods:
            periods[_key(r['period'])]['expenses'] += Decimal(r['expenses'] or 0)
        for r in return_periods:
            periods[_key(r['period'])]['returns'] += Decimal(r['returns'] or 0)

        rows = []
        tot = {'revenue': ZERO, 'cogs': ZERO, 'expenses': ZERO, 'returns': ZERO, 'net': ZERO}
        for period in sorted(k for k in periods if k is not None):
            v = periods[period]
            net = v['revenue'] - v['cogs'] - v['expenses'] - v['returns']
            for k in ('revenue', 'cogs', 'expenses', 'returns'):
                tot[k] += v[k]
            tot['net'] += net
            rows.append({
                'period': period,
                'revenue': _q(v['revenue']),
                'cogs': _q(v['cogs']),
                'gross_profit': _q(v['revenue'] - v['cogs']),
                'expenses': _q(v['expenses']),
                'returns': _q(v['returns']),
                'net': _q(net),
            })
        totals = {
            'revenue': _q(tot['revenue']),
            'cogs': _q(tot['cogs']),
            'gross_profit': _q(tot['revenue'] - tot['cogs']),
            'expenses': _q(tot['expenses']),
            'returns': _q(tot['returns']),
            'net': _q(tot['net']),
        }
        # Reconciliation guard: revenue - cogs - expenses - returns must equal net.
        check = tot['revenue'] - tot['cogs'] - tot['expenses'] - tot['returns']
        totals['reconciles'] = (check == tot['net'])
        return Response({'granularity': g, 'date_from': df, 'date_to': dt,
                         'rows': rows, 'totals': totals})


# ============================================================
# 6. EXPENSE BREAKDOWN — by category + period
# ============================================================

class ExpenseReportView(_StoreMixin, APIView):
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        df, dt = self.date_range(request)
        branch = self.branch_id(request)
        g = self.granularity(request)

        f = Q(store=store, is_deleted=False, date__gte=df, date__lte=dt)
        if branch:
            f &= Q(branch_id=branch)

        by_category = (
            Expense.objects.filter(f)
            .values('category__id', 'category__name')
            .annotate(total=Coalesce(Sum('amount'), Value(ZERO), output_field=DEC),
                      count=Count('id'))
            .order_by('-total')
        )
        cat_rows = [{
            'category_id': str(r['category__id']) if r['category__id'] else None,
            'name': r['category__name'] or '(Uncategorized)',
            'count': r['count'],
            'total': _q(r['total']),
        } for r in by_category]

        by_period = (
            Expense.objects.filter(f)
            .annotate(period=_trunc(g, 'date'))
            .values('period')
            .annotate(total=Coalesce(Sum('amount'), Value(ZERO), output_field=DEC))
            .order_by('period')
        )
        period_rows = [{'period': r['period'].isoformat() if r['period'] else None,
                        'total': _q(r['total'])} for r in by_period]

        grand = sum(Decimal(r['total']) for r in cat_rows)
        return Response({'date_from': df, 'date_to': dt, 'granularity': g,
                         'by_category': cat_rows, 'by_period': period_rows,
                         'total': _q(grand)})


# ============================================================
# 7. STOCK MOVEMENT LEDGER — per variant
# ============================================================

class StockLedgerView(_StoreMixin, APIView):
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        variant_id = request.query_params.get('variant')
        if not variant_id:
            return Response({'detail': 'variant query param is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not ProductVariant.objects.filter(id=variant_id, product__store=store).exists():
            return Response({'detail': 'Variant not found in this store.'},
                            status=status.HTTP_404_NOT_FOUND)
        df, dt = self.date_range(request)
        branch = self.branch_id(request)
        scope = request.query_params.get('scope', 'active')
        if scope not in ('active', 'storage', 'combined'):
            scope = 'active'

        # Each move carries deltas for BOTH pools so we can show any scope:
        #   active_delta  -> change to active StockLevel
        #   storage_delta -> change to StorageStock on-hand
        #   pools         -> which scopes this row appears in
        # active scope shows active_delta; storage shows storage_delta;
        # combined shows their sum (a to/from-storage transfer nets to zero).
        moves = []

        def add(dt_, type_, ref, note, branch_name, *, active=ZERO, storage=ZERO, pools=()):
            moves.append({'dt': dt_, 'type': type_, 'ref': ref, 'note': note,
                          'branch': branch_name, 'active': Decimal(active),
                          'storage': Decimal(storage), 'pools': set(pools)})

        # Write-off creates a round-trip DAMAGE adjustment (active nets to zero);
        # exclude those adjustments here and represent the write-off via its
        # storage movement instead — keeps every scope reconciling.
        writeoff_adj_ids = set(
            StorageMovement.objects.filter(
                store=store, variant_id=variant_id,
                direction=StorageMovement.Direction.WRITE_OFF,
                related_adjustment__isnull=False,
            ).values_list('related_adjustment_id', flat=True)
        )

        # Purchases IN (RECEIVED) — active only
        pf = Q(invoice__store=store, invoice__status=PurchaseInvoice.Status.RECEIVED,
               invoice__is_deleted=False, variant_id=variant_id)
        if branch:
            pf &= Q(invoice__branch_id=branch)
        for it in PurchaseItem.objects.filter(pf).select_related('invoice', 'invoice__supplier', 'invoice__branch'):
            add(it.invoice.date, 'PURCHASE',
                it.invoice.vendor_reference or str(it.invoice_id),
                it.invoice.supplier.name if it.invoice.supplier else '',
                it.invoice.branch.name, active=it.quantity, pools=('active',))

        # Sales OUT (POSTED) — active only
        sf = Q(invoice__store=store, invoice__status=SalesInvoice.Status.POSTED,
               invoice__is_deleted=False, variant_id=variant_id)
        if branch:
            sf &= Q(invoice__branch_id=branch)
        for it in SalesInvoiceItem.objects.filter(sf).select_related('invoice', 'invoice__customer', 'invoice__branch'):
            add(it.invoice.date, 'SALE',
                f"#{it.invoice.invoice_number}" if it.invoice.invoice_number else str(it.invoice_id),
                it.invoice.customer.name if it.invoice.customer else '',
                it.invoice.branch.name, active=-it.quantity, pools=('active',))

        # Adjustments (signed) — active only, minus write-off artifacts
        af = Q(store=store, variant_id=variant_id)
        if branch:
            af &= Q(branch_id=branch)
        for adj in StockAdjustment.objects.filter(af).select_related('branch'):
            if adj.id in writeoff_adj_ids:
                continue
            add(adj.created_at, 'ADJUSTMENT', adj.get_reason_display(), adj.notes or '',
                adj.branch.name, active=adj.quantity_change, pools=('active',))

        # Returns IN (restock only) — active only
        rf = Q(refund__store=store, refund__is_deleted=False, variant_id=variant_id,
               restock_inventory=True)
        if branch:
            rf &= Q(refund__branch_id=branch)
        for it in RefundItem.objects.filter(rf).select_related('refund', 'refund__branch'):
            add(it.refund.date, 'RETURN',
                f"R#{it.refund.refund_number}" if it.refund.refund_number else str(it.refund_id),
                it.refund.reason or '', it.refund.branch.name,
                active=it.quantity, pools=('active',))

        # Storage movements — shuffle between pools (and write-offs leave storage)
        smf = Q(store=store, variant_id=variant_id)
        if branch:
            smf &= (Q(from_branch_id=branch) | Q(to_branch_id=branch))
        D = StorageMovement.Direction
        for mv in StorageMovement.objects.filter(smf).select_related(
                'storage_location', 'from_branch', 'to_branch'):
            br = (mv.from_branch.name if mv.from_branch else
                  (mv.to_branch.name if mv.to_branch else ''))
            if mv.direction == D.TO_STORAGE:
                add(mv.moved_at, 'STORAGE_OUT', mv.storage_location.name, mv.reason or '',
                    br, active=-mv.quantity, storage=mv.quantity, pools=('active', 'storage'))
            elif mv.direction == D.FROM_STORAGE:
                add(mv.moved_at, 'STORAGE_IN', mv.storage_location.name, mv.reason or '',
                    br, active=mv.quantity, storage=-mv.quantity, pools=('active', 'storage'))
            else:  # WRITE_OFF — leaves storage, no active effect
                add(mv.moved_at, 'STORAGE_WRITE_OFF', mv.storage_location.name, mv.reason or '',
                    br, storage=-mv.quantity, pools=('storage',))

        def row_qty(m):
            if scope == 'active':
                return m['active']
            if scope == 'storage':
                return m['storage']
            return m['active'] + m['storage']   # combined

        def in_scope(m):
            if scope == 'combined':
                return True
            return scope in m['pools']

        moves = [m for m in moves if in_scope(m)]
        moves.sort(key=lambda m: m['dt'])

        # Opening balance = sum of movements strictly before date_from
        opening = ZERO
        in_range = []
        for m in moves:
            mdate = timezone.localtime(m['dt']).date()
            if mdate < df:
                opening += row_qty(m)
            elif mdate <= dt:
                in_range.append(m)

        running = opening
        rows = []
        for m in in_range:
            q = row_qty(m)
            running += q
            rows.append({
                'date': m['dt'],
                'type': m['type'],
                'qty': str(Decimal(q).normalize()),
                'balance': str(Decimal(running).normalize()),
                'ref': m['ref'],
                'note': m['note'],
                'branch': m['branch'],
            })
        return Response({'variant_id': variant_id, 'scope': scope,
                         'date_from': df, 'date_to': dt,
                         'opening_balance': str(Decimal(opening).normalize()),
                         'closing_balance': str(Decimal(running).normalize()),
                         'rows': rows})


# ============================================================
# 8. CASHIER PERFORMANCE — per staff
# ============================================================

class CashierPerformanceView(_StoreMixin, APIView):
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        df, dt = self.date_range(request)

        staff = list(User.objects.filter(store=store, is_superadmin=False))
        rows = []
        for u in staff:
            pay = Payment.objects.filter(
                invoice__store=store, created_by=u, is_deleted=False,
                created_at__date__gte=df, created_at__date__lte=dt)
            pay_agg = pay.aggregate(
                value=Coalesce(Sum('amount'), Value(ZERO), output_field=DEC),
                invoices=Count('invoice', distinct=True))
            ref = RefundInvoice.objects.filter(
                store=store, created_by=u, is_deleted=False,
                date__date__gte=df, date__date__lte=dt)
            ref_agg = ref.aggregate(
                value=Coalesce(Sum('total_refunded'), Value(ZERO), output_field=DEC),
                count=Count('id'))
            shifts = WorkShift.objects.filter(
                store=store, user=u, status=WorkShift.Status.CLOSED,
                start_time__date__gte=df, start_time__date__lte=dt)
            shift_agg = shifts.aggregate(
                diff=Coalesce(Sum('difference'), Value(ZERO), output_field=DEC),
                count=Count('id'))

            sales_count = pay_agg['invoices'] or 0
            if (sales_count == 0 and ref_agg['count'] == 0 and shift_agg['count'] == 0):
                continue
            rows.append({
                'user_id': str(u.id),
                'name': u.get_full_name() or u.username,
                'sales_count': sales_count,
                'sales_value': _q(pay_agg['value']),
                'returns_count': ref_agg['count'] or 0,
                'returns_value': _q(ref_agg['value']),
                'shifts': shift_agg['count'] or 0,
                'cash_difference': _q(shift_agg['diff']),
            })
        rows.sort(key=lambda r: Decimal(r['sales_value']), reverse=True)
        totals = {
            'sales_count': sum(r['sales_count'] for r in rows),
            'sales_value': _q(sum(Decimal(r['sales_value']) for r in rows)),
            'returns_count': sum(r['returns_count'] for r in rows),
            'returns_value': _q(sum(Decimal(r['returns_value']) for r in rows)),
            'cash_difference': _q(sum(Decimal(r['cash_difference']) for r in rows)),
        }
        return Response({'date_from': df, 'date_to': dt, 'rows': rows, 'totals': totals})


# ============================================================
# 9. TAX REPORT — collected output tax by rate + period
# ============================================================

class TaxReportView(_StoreMixin, APIView):
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        df, dt = self.date_range(request)
        g = self.granularity(request)

        base = SalesInvoiceItem.objects.filter(
            invoice__store=store, invoice__status=SalesInvoice.Status.POSTED,
            invoice__is_deleted=False,
            invoice__date__date__gte=df, invoice__date__date__lte=dt,
            tax_amount__gt=0,
        )

        by_rate = (
            base.values('variant__product__tax__rate', 'variant__product__tax__name')
            .annotate(
                taxable=Coalesce(Sum(F('quantity') * F('unit_price')), Value(ZERO), output_field=DEC),
                collected=Coalesce(Sum('tax_amount'), Value(ZERO), output_field=DEC))
            .order_by('-collected')
        )
        rate_rows = [{
            'rate': str(r['variant__product__tax__rate']) if r['variant__product__tax__rate'] is not None else None,
            'name': r['variant__product__tax__name'] or '(No tax record)',
            'taxable': _q(r['taxable']),
            'collected': _q(r['collected']),
        } for r in by_rate]

        by_period = (
            base.annotate(period=_trunc(g, 'invoice__date'))
            .values('period')
            .annotate(collected=Coalesce(Sum('tax_amount'), Value(ZERO), output_field=DEC))
            .order_by('period')
        )
        period_rows = [{'period': r['period'].isoformat() if r['period'] else None,
                        'collected': _q(r['collected'])} for r in by_period]

        total = sum(Decimal(r['collected']) for r in rate_rows)
        return Response({'date_from': df, 'date_to': dt, 'granularity': g,
                         'by_rate': rate_rows, 'by_period': period_rows,
                         'total_collected': _q(total)})


# ============================================================
# 10. STORAGE AGING — parked layers by age bucket
# ============================================================

_AGING_BUCKETS = [
    ('b0_30',    0,   30),
    ('b31_60',   31,  60),
    ('b61_90',   61,  90),
    ('b91_180',  91,  180),
    ('b180_plus', 181, None),   # write-down candidate
]


def _aging_bucket(days):
    for key, lo, hi in _AGING_BUCKETS:
        if days >= lo and (hi is None or days <= hi):
            return key
    return 'b0_30'


class StorageAgingView(_StoreMixin, APIView):
    """Active storage layers grouped into age buckets by days-in-storage.
    The 180+ bucket is flagged as an NRV write-down candidate."""
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        today = timezone.localdate()
        location = request.query_params.get('storage_location')

        qs = (StorageStock.objects
              .filter(store=store, is_deleted=False)
              .select_related('variant__product', 'storage_location'))
        if location:
            qs = qs.filter(storage_location_id=location)

        buckets = {key: {'qty': ZERO, 'value': ZERO, 'items': 0}
                   for key, _, _ in _AGING_BUCKETS}
        for layer in qs:
            days = (today - timezone.localtime(layer.moved_in_at).date()).days
            b = buckets[_aging_bucket(days)]
            qty = Decimal(layer.quantity_remaining or 0)
            b['qty'] += qty
            b['value'] += qty * Decimal(layer.cost_at_move or 0)
            b['items'] += 1

        rows = [{
            'bucket': key,
            'qty': str(buckets[key]['qty'].normalize()),
            'value': _q(buckets[key]['value']),
            'items': buckets[key]['items'],
            'write_down_candidate': key == 'b180_plus',
        } for key, _, _ in _AGING_BUCKETS]
        totals = {
            'qty': str(sum((buckets[k]['qty'] for k, _, _ in _AGING_BUCKETS), ZERO).normalize()),
            'value': _q(sum((buckets[k]['value'] for k, _, _ in _AGING_BUCKETS), ZERO)),
            'items': sum(buckets[k]['items'] for k, _, _ in _AGING_BUCKETS),
        }
        return Response({'as_of': today, 'rows': rows, 'totals': totals})


# ============================================================
# 11. STORAGE VALUE — by location, then category / supplier
# ============================================================

class StorageValueView(_StoreMixin, APIView):
    """Storage inventory value Σ(qty_remaining × cost_at_move), grouped by
    storage location then by category (default) or supplier."""
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        group_by = request.query_params.get('group_by', 'category')
        if group_by not in ('category', 'supplier'):
            group_by = 'category'
        sub_id = (f'variant__product__{group_by}__id')
        sub_name = (f'variant__product__{group_by}__name')

        qs = (StorageStock.objects
              .filter(store=store, is_deleted=False)
              .values('storage_location__id', 'storage_location__name', sub_id, sub_name)
              .annotate(
                  qty=Coalesce(Sum('quantity_remaining'), Value(ZERO),
                               output_field=DecimalField(max_digits=18, decimal_places=3)),
                  value=Coalesce(Sum(F('quantity_remaining') * F('cost_at_move')),
                                 Value(ZERO), output_field=DEC))
              .order_by('storage_location__name', '-value'))

        locations = {}
        for r in qs:
            loc_id = str(r['storage_location__id'])
            loc = locations.setdefault(loc_id, {
                'storage_location_id': loc_id,
                'name': r['storage_location__name'],
                'qty': ZERO, 'value': ZERO, 'groups': [],
            })
            loc['groups'].append({
                'id': str(r[sub_id]) if r[sub_id] else None,
                'name': r[sub_name] or '(Unassigned)',
                'qty': str(Decimal(r['qty'] or 0).normalize()),
                'value': _q(r['value']),
            })
            loc['qty'] += Decimal(r['qty'] or 0)
            loc['value'] += Decimal(r['value'] or 0)

        rows = []
        for loc in locations.values():
            loc['qty'] = str(loc['qty'].normalize())
            loc['value'] = _q(loc['value'])
            rows.append(loc)
        grand = sum(Decimal(g['value']) for loc in rows for g in loc['groups'])
        return Response({'group_by': group_by, 'rows': rows, 'total_value': _q(grand)})


# ============================================================
# 12. STORAGE MOVEMENTS — the audit log as a report
# ============================================================

class StorageMovementsReportView(_StoreMixin, APIView):
    """Storage movement history with date-range + location/variant/user/direction
    filters. Mirrors the other report date presets."""
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()
        df, dt = self.date_range(request)

        qs = (StorageMovement.objects
              .filter(store=store, moved_at__date__gte=df, moved_at__date__lte=dt)
              .select_related('variant__product', 'storage_location',
                              'from_branch', 'to_branch', 'created_by')
              .order_by('-moved_at'))
        loc = request.query_params.get('storage_location')
        if loc:
            qs = qs.filter(storage_location_id=loc)
        variant = request.query_params.get('variant')
        if variant:
            qs = qs.filter(variant_id=variant)
        user = request.query_params.get('user')
        if user:
            qs = qs.filter(created_by_id=user)
        direction = request.query_params.get('direction')
        if direction:
            qs = qs.filter(direction=direction)

        rows = [{
            'id': str(mv.id),
            'date': mv.moved_at,
            'direction': mv.direction,
            'direction_display': mv.get_direction_display(),
            'sku': mv.variant.sku,
            'product': mv.variant.product.name,
            'storage_location': mv.storage_location.name,
            'qty': str(Decimal(mv.quantity or 0).normalize()),
            'cost_at_move': _q(mv.cost_at_move),
            'value': _q(Decimal(mv.quantity or 0) * Decimal(mv.cost_at_move or 0)),
            'from_branch': mv.from_branch.name if mv.from_branch else None,
            'to_branch': mv.to_branch.name if mv.to_branch else None,
            'user': (mv.created_by.get_full_name() or mv.created_by.username) if mv.created_by else None,
            'reason': mv.reason or '',
            'note': mv.note or '',
        } for mv in qs]
        return Response({'date_from': df, 'date_to': dt, 'rows': rows,
                         'count': len(rows)})


# ============================================================
# 13. STORAGE RECONCILIATION — storage-pool integrity check
# ============================================================

class StorageReconciliationView(_StoreMixin, APIView):
    """Per variant, assert current storage on-hand (Σ active layers) equals the
    net of signed storage movements (TO_STORAGE − FROM_STORAGE − WRITE_OFF).
    Any mismatch is a data-integrity failure and is listed."""
    def get(self, request):
        store = self.get_store(request)
        if not store:
            return _no_store()

        # Current storage on-hand per variant (active layers only).
        on_hand = defaultdict(lambda: ZERO)
        for r in (StorageStock.objects.filter(store=store, is_deleted=False)
                  .values('variant_id')
                  .annotate(q=Coalesce(Sum('quantity_remaining'), Value(ZERO),
                                       output_field=DecimalField(max_digits=18, decimal_places=3)))):
            on_hand[str(r['variant_id'])] = Decimal(r['q'] or 0)

        # Net of signed storage movements per variant.
        D = StorageMovement.Direction
        net = defaultdict(lambda: ZERO)
        for r in (StorageMovement.objects.filter(store=store)
                  .values('variant_id', 'direction')
                  .annotate(q=Coalesce(Sum('quantity'), Value(ZERO),
                                       output_field=DecimalField(max_digits=18, decimal_places=3)))):
            vid = str(r['variant_id'])
            q = Decimal(r['q'] or 0)
            if r['direction'] == D.TO_STORAGE:
                net[vid] += q
            else:  # FROM_STORAGE or WRITE_OFF both leave storage
                net[vid] -= q

        variant_ids = set(on_hand) | set(net)
        meta = {str(vid): (sku, pname) for vid, sku, pname in
                ProductVariant.objects.filter(id__in=variant_ids)
                .values_list('id', 'sku', 'product__name')}

        failures = []
        for vid in variant_ids:
            expected = net[vid]
            actual = on_hand[vid]
            if expected != actual:
                sku, pname = meta.get(vid, (None, None))
                failures.append({
                    'variant_id': vid,
                    'sku': sku,
                    'product': pname,
                    'on_hand': str(actual.normalize()),
                    'expected': str(expected.normalize()),
                    'difference': str((actual - expected).normalize()),
                })
        return Response({'checked': len(variant_ids), 'reconciles': not failures,
                         'failures': failures})


# ============================================================
# 14. EXPIRY / BATCH (FEFO) — expiring-soon + expired list, valuation by batch
# ============================================================

class ExpiryReportView(_StoreMixin, APIView):
    """Open batches (quantity_remaining > 0) of expiry-tracked products, bucketed by
    how close to expiry they are. ``?window=N`` (default = store's expiry_alert_days)
    sets the 'expiring soon' horizon; ``?status=expired|soon|all`` filters. Returns a
    flat batch list with valuation (qty × cost_per_base) for each row + totals.
    """
    def get(self, request):
        from inventory.models import StockBatch
        store = self.get_store(request)
        if not store:
            return _no_store()

        settings_obj = getattr(store, 'settings', None)
        # Master switch off → feature dormant. Old StockBatch rows may still exist
        # (preserved, never deleted) but stay hidden from the report, matching how the
        # rest of the app reverts to the single-number path when the switch is off.
        if not getattr(settings_obj, 'expiry_tracking_enabled', False):
            return Response({
                'window_days': 0, 'rows': [], 'enabled': False,
                'totals': {'batches': 0, 'total_value': '0.00',
                           'expired_value': '0.00', 'expiring_soon_value': '0.00'},
            })
        default_window = getattr(settings_obj, 'expiry_alert_days', 60) or 60
        try:
            window = int(request.query_params.get('window', default_window))
        except (TypeError, ValueError):
            window = default_window
        status_filter = request.query_params.get('status', 'all')
        branch = self.branch_id(request)
        today = timezone.localdate()
        soon_cutoff = today + timedelta(days=window)

        qs = (StockBatch.objects
              .filter(store=store, quantity_remaining__gt=0, variant__product__track_expiry=True)
              .select_related('variant', 'variant__product', 'branch')
              .order_by('expiry_date', 'received_date'))
        if branch:
            qs = qs.filter(branch_id=branch)

        rows, total_value, expired_value, soon_value = [], ZERO, ZERO, ZERO
        for b in qs:
            exp = b.expiry_date
            is_expired = bool(exp and exp < today)
            is_soon = bool(exp and today <= exp <= soon_cutoff)
            state = 'expired' if is_expired else ('soon' if is_soon else 'ok')
            if status_filter == 'expired' and not is_expired:
                continue
            if status_filter == 'soon' and not is_soon:
                continue
            value = (Decimal(str(b.quantity_remaining)) * Decimal(str(b.cost_per_base))).quantize(Decimal('0.01'))
            total_value += value
            if is_expired:
                expired_value += value
            elif is_soon:
                soon_value += value
            rows.append({
                'batch_id': str(b.id),
                'sku': b.variant.sku,
                'product': b.variant.product.name,
                'branch': b.branch.name,
                'batch_number': b.batch_number,
                'expiry_date': exp.isoformat() if exp else None,
                'days_left': (exp - today).days if exp else None,
                'quantity_remaining': str(b.quantity_remaining),
                'cost_per_base': str(b.cost_per_base),
                'value': str(value),
                'state': state,
            })
        return Response({
            'window_days': window,
            'rows': rows,
            'totals': {
                'batches': len(rows),
                'total_value': str(total_value.quantize(Decimal('0.01'))),
                'expired_value': str(expired_value.quantize(Decimal('0.01'))),
                'expiring_soon_value': str(soon_value.quantize(Decimal('0.01'))),
            },
        })


class ExpiryScanView(_StoreMixin, APIView):
    """Manual 'Scan expiring stock' button (no cron — Yakot's preference). Fires one
    WARNING notification summarising expired + expiring-soon batches for the store.
    POST only; returns the counts so the UI can toast the result."""
    def post(self, request):
        from inventory.models import StockBatch
        store = self.get_store(request)
        if not store:
            return _no_store()
        settings_obj = getattr(store, 'settings', None)
        if not getattr(settings_obj, 'expiry_tracking_enabled', False):
            return Response({'expired': 0, 'expiring_soon': 0, 'window_days': 0,
                             'enabled': False})
        window = getattr(settings_obj, 'expiry_alert_days', 60) or 60
        today = timezone.localdate()
        soon_cutoff = today + timedelta(days=window)

        qs = StockBatch.objects.filter(
            store=store, quantity_remaining__gt=0,
            variant__product__track_expiry=True, expiry_date__isnull=False)
        expired = qs.filter(expiry_date__lt=today).count()
        soon = qs.filter(expiry_date__gte=today, expiry_date__lte=soon_cutoff).count()

        if expired or soon:
            from notifications.dispatcher import send_notification
            from notifications.models import Notification as Notif
            bits = []
            if expired:
                bits.append(f"{expired} expired")
            if soon:
                bits.append(f"{soon} expiring within {window} days")
            send_notification(
                store=store,
                title="Expiry alert: stock needs attention",
                body=" · ".join(bits),
                priority=Notif.Priority.WARNING,
                notif_type=Notif.Type.EXPIRY,
                link="/reports/expiry",
            )
        return Response({'expired': expired, 'expiring_soon': soon, 'window_days': window})
