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
from inventory.models import StockAdjustment, ProductVariant

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

        moves = []  # each: {datetime, type, qty (signed), ref, note, branch}

        # Purchases IN (RECEIVED)
        pf = Q(invoice__store=store, invoice__status=PurchaseInvoice.Status.RECEIVED,
               invoice__is_deleted=False, variant_id=variant_id)
        if branch:
            pf &= Q(invoice__branch_id=branch)
        for it in PurchaseItem.objects.filter(pf).select_related('invoice', 'invoice__supplier', 'invoice__branch'):
            moves.append({'dt': it.invoice.date, 'type': 'PURCHASE', 'qty': it.quantity,
                          'ref': it.invoice.vendor_reference or str(it.invoice_id),
                          'note': it.invoice.supplier.name if it.invoice.supplier else '',
                          'branch': it.invoice.branch.name})

        # Sales OUT (POSTED)
        sf = Q(invoice__store=store, invoice__status=SalesInvoice.Status.POSTED,
               invoice__is_deleted=False, variant_id=variant_id)
        if branch:
            sf &= Q(invoice__branch_id=branch)
        for it in SalesInvoiceItem.objects.filter(sf).select_related('invoice', 'invoice__customer', 'invoice__branch'):
            moves.append({'dt': it.invoice.date, 'type': 'SALE', 'qty': -it.quantity,
                          'ref': f"#{it.invoice.invoice_number}" if it.invoice.invoice_number else str(it.invoice_id),
                          'note': it.invoice.customer.name if it.invoice.customer else '',
                          'branch': it.invoice.branch.name})

        # Adjustments (signed)
        af = Q(store=store, variant_id=variant_id)
        if branch:
            af &= Q(branch_id=branch)
        for adj in StockAdjustment.objects.filter(af).select_related('branch'):
            moves.append({'dt': adj.created_at, 'type': 'ADJUSTMENT', 'qty': adj.quantity_change,
                          'ref': adj.get_reason_display(), 'note': adj.notes or '',
                          'branch': adj.branch.name})

        # Returns IN (restock only)
        rf = Q(refund__store=store, refund__is_deleted=False, variant_id=variant_id,
               restock_inventory=True)
        if branch:
            rf &= Q(refund__branch_id=branch)
        for it in RefundItem.objects.filter(rf).select_related('refund', 'refund__branch'):
            moves.append({'dt': it.refund.date, 'type': 'RETURN', 'qty': it.quantity,
                          'ref': f"R#{it.refund.refund_number}" if it.refund.refund_number else str(it.refund_id),
                          'note': it.refund.reason or '', 'branch': it.refund.branch.name})

        moves.sort(key=lambda m: m['dt'])

        # Opening balance = sum of movements strictly before date_from
        opening = ZERO
        in_range = []
        for m in moves:
            mdate = timezone.localtime(m['dt']).date()
            if mdate < df:
                opening += m['qty']
            elif mdate <= dt:
                in_range.append(m)

        running = opening
        rows = []
        for m in in_range:
            running += m['qty']
            rows.append({
                'date': m['dt'],
                'type': m['type'],
                'qty': str(Decimal(m['qty']).normalize()),
                'balance': str(Decimal(running).normalize()),
                'ref': m['ref'],
                'note': m['note'],
                'branch': m['branch'],
            })
        return Response({'variant_id': variant_id, 'date_from': df, 'date_to': dt,
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
