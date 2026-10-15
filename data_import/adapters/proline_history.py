"""Adapter: the Proline desktop ERP — part B, HISTORY (sales, purchases and their returns).

Mixed into ProlineAdapter (`proline.py`), so ONE `import_external proline …` run does part A then part B in the same
transaction (`--part master|history|all`). The design and every money / stock rule: PROLINE-IMPORT-B.md (repo root).

The iron rule here: HISTORY NEVER MOVES TODAY'S MONEY OR STOCK. Rows are created directly — never through the till's
checkout, the invoice serializers or purchase_returns.py. Sales are born POSTED and purchases RECEIVED, so the two stock
signals (they act on DRAFT → POSTED / RECEIVED of an existing row) never fire; lines are bulk-created (no save() side
effects); return lines never restock. No Payment, no box line, no shift. History is SETTLED: a sale is paid in full, a
return paid back in full, and imported purchases / supplier returns stay out of the supplier's running account
(`source_number` set). The debt at switch-over comes in once, through part A's --balances=opening.

What lands where (tags = ExternalRef kinds):
    tb_InvoOut + tb_InvoOutData           → SalesInvoice POSTED + lines            sale = InvoNo
        the lines with Back = y (Qty < 0) → RefundInvoice (free return) + lines  sale_return = InvoNo
    tb_InvoIn + tb_InvoInData             → PurchaseInvoice RECEIVED + lines       purchase = InvoNo
    tb_InvoInBack + tb_InvoInDataBack     → PurchaseReturn POSTED + lines          purchase_return = InvoNo@date
Counted only, never imported: serials (each device has its own item code; the lines' Serial cell says "NO"),
tb_Serial (the repair desk's device log), reservations (all converted to sales — StoreState out), tb_BankChecks
(instalment notes on customers — a future feature), held sales (tb_WaitingInvoOut).
Report samples name documents by NUMBER only.
"""
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

from django.utils import timezone

from core.models import Branch
from finance.models import (InvoiceSequence, PurchaseInvoice, PurchaseItem, PurchaseReturn, PurchaseReturnItem,
                            RefundInvoice, RefundItem, SalesInvoice, SalesInvoiceItem)
from inventory.models import ProductVariant, Supplier
from users.models import Customer

from ..core import BadNumber, norm_key, parse_decimal, read_table, text, truthy
from ..models import ExternalRef

CREATED = ExternalRef.Origin.CREATED
CENT = Decimal('0.01')
ZERO = Decimal('0')

INVO_IN_DATA_BACK_COLUMNS = [
    'InvoNo', 'Date', 'SN', 'ComCode', 'ComName', 'ItemCode', 'ItemName', 'Unit', 'Qty', 'Price', 'Total',
    'Group1Code', 'Group2Code', 'SourceCode', 'Serial', 'ExDate', 'ExDateName', 'BackArabic', 'StoreCode', 'StoreName',
    'Group0Code', 'SerialID', 'MotorID', 'Note', 'BranshCode', 'BranshName', 'Motor', 'UserAdded', 'PaymentTypeAr',
    'RefCode', 'RefName', 'PriceBefore', 'Unit2', 'Qty2']
SERIAL_COLUMNS = ['InvoNo', 'InvoDate', 'StoreState', 'ItemCode', 'ItemName', 'Serial', 'InvoType', 'SerialID',
                  'FormType', 'sIn', 'sOut', 'StoreCode', 'ItemService']
RES_ORDER_COLUMNS = [
    'OrderNo', 'inDate', 'BranchCode', 'BranchName', 'StoreCode', 'StoreName', 'UserIn', 'UserOut', 'CustomerCode',
    'CustomerName', 'PaymentTypeEn', 'PaymentTypeAr', 'Data', 'outDate', 'StoreState', 'Total', 'DownPayment',
    'Remaining', 'PriceType', 'Note', 'UserSerID', 'UserSerName', 'UserPayID', 'UserPayName', 'RefCode', 'RefName',
    'DiscountTotal']
RES_LINE_COLUMNS = [
    'InvoNo', 'DateIn', 'DateArr', 'SN', 'ComCode', 'ComName', 'ItemCode', 'ItemName', 'Unit', 'MadeIn', 'ItemEdit',
    'Qty', 'BackArabic', 'Back', 'Price', 'Total', 'Group1Code', 'Group2Code', 'SourceCode', 'Serial', 'ExDate',
    'ExDateName', 'UserAdded', 'ReservationState', 'Dayss', 'BranchCode', 'Motor', 'Notes', 'BranchName', 'Discount',
    'PriceBefore', 'StoreCode', 'StoreName', 'Serv', 'UserPayIDServ', 'UserPayNameServ', 'UserPayIDSale',
    'UserPayNameSale', 'DiscountRates', 'SubItem', 'STaxe', 'Group0Code', 'Chk_Taxe', 'GTaxe', 'Service', 'Delivery',
    'Mob_Print', 'SerialID', 'MotorID', 'Note', 'Consignment', 'Cost', 'ItemNameInvoice', 'Unit2', 'Qty2']
CHEQUE_COLUMNS = [
    'id', 'DateAdded', 'UserAdded', 'AccType', 'InstallMethod', 'ComCode', 'ComName', 'BranchCode', 'BranchName',
    'RefCode', 'RefName', 'DateDueType', 'Sn', 'DateDue', 'DateEnd', 'Amount', 'Check_Num', 'Check_BankName',
    'Check_Note', 'Check_Status', 'Alert', 'Alert_NextTime']
HISTORY_TABLES = {'tb_InvoInDataBack': INVO_IN_DATA_BACK_COLUMNS, 'tb_Serial': SERIAL_COLUMNS,
                  'tb_Reservation_Order': RES_ORDER_COLUMNS, 'tb_Reservation': RES_LINE_COLUMNS,
                  'tb_BankChecks': CHEQUE_COLUMNS, 'tb_WaitingInvoOut': ()}

CREDIT_SALE_TYPES = ('اجل',)                  # tb_InvoOut.InvoType of a credit (Ajel) sale


class _Skip(Exception):
    """One source row cannot be imported — the message is the report's reason."""


def _money(value):
    return (value or ZERO).quantize(CENT)


class HistoryMixin:
    """Part B of the Proline adapter. Needs part A's load (party key column, tables) and its tags (in the same run, or
    an earlier committed one)."""

    # ── load: read + count + blockers (no writes) ────────────────────────────────────────────────────────────────────
    def load_history(self):
        o, r = self.options, self.report
        for key, title in (('sales', 'Sales (tb_InvoOut + tb_InvoOutData)'),
                           ('sale_returns', 'Returned lines on sales (Back = y) → returns'),
                           ('purchases', 'Purchases (tb_InvoIn + tb_InvoInData)'),
                           ('purchase_returns', 'Returns to suppliers (tb_InvoInBack + tb_InvoInDataBack)')):
            r.section(key, title)
        for name, cols in HISTORY_TABLES.items():
            self.t[name] = read_table(self.folder, name, cols)
            r.file(name, self.t[name])
        self.history_facts = r.section('history', 'History — rules and facts (decides later work)', info=True)
        self.history_facts.line('History is imported SETTLED: every sale paid in full, every return paid back, imported '
                                'purchases / supplier returns kept out of the supplier account. No stock, no payments, '
                                'no box lines, no shift counts. Today\'s debts come in once via --balances=opening.')
        self.number_mode = o.get('invoice_numbers') or 'proline'
        self.branch = (Branch.objects.filter(store=self.store, is_main_branch=True).first()
                       or Branch.objects.filter(store=self.store).order_by('created_at').first())
        if self.branch is None:
            r.blockers.append('The shop has no branch — history needs one to land in.')
        self.walk_in = Customer.all_objects.filter(store=self.store, is_walk_in=True).first()
        if self.walk_in is None and self._rows('tb_InvoOut'):
            r.blockers.append('The shop has no walk-in customer (cash sales with no customer go to it).')
        waiting = self._rows('tb_WaitingInvoOut')
        if waiting:
            self.history_facts.note('held (waiting) sales in tb_WaitingInvoOut — NOT imported (lines / invoices)',
                                    len(waiting))
            self.history_facts.note('  … distinct held invoices', len({text(x.get('InvoNo')) for x in waiting}))
        for name in ('tb_InvoOut', 'tb_InvoIn'):
            for col in ('StoreCode', 'BranchCode'):
                seen = Counter(text(x.get(col)) or '(empty)' for x in self._rows(name))
                if seen:
                    self.history_facts.line(f'{name}.{col} values (all land in branch "{self.branch.name if self.branch else "—"}"): '
                                            + ', '.join(f'{v} × {n}' for v, n in seen.most_common(10)))

    # ── shared helpers ───────────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _when(raw):
        """'2018-09-16 13:29:09' (optionally .fff) → an aware datetime in the shop's time zone; None when empty."""
        s = text(raw)
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace('T', ' ')[:26])
        except ValueError:
            raise _Skip(f'bad date "{s[:30]}"')
        return timezone.make_aware(dt) if timezone.is_naive(dt) else dt

    @staticmethod
    def _num(x, col):
        try:
            return parse_decimal(x.get(col), col)
        except BadNumber as exc:
            raise _Skip(f'bad number in {exc.column}')

    def _tag_objects(self, kind, model, live_filter=None):
        """{source key: object} for this shop's tags of `kind` (soft-deleted objects included — the caller decides)."""
        ids = {}
        for (k, key), ref in self.refs._refs.items():
            if k == kind:
                ids[ref.object_id] = key
        if not ids:
            return {}
        qs = model.all_objects.filter(pk__in=list(ids), store=self.store)
        if live_filter is not None:
            qs = qs.select_related(*live_filter)
        return {ids[o.pk]: o for o in qs}

    def _customer(self, code):
        """→ (customer, how) for a tb_Cust code via part A's tag. how: 'tag' / 'deleted' / 'none'."""
        key = self._tag_key(norm_key(code))
        c = self._customers.get(key)
        if c is None:
            return None, 'none'
        if c.is_deleted:
            return None, 'deleted'
        return c, 'tag'

    def _supplier(self, code):
        s = self._suppliers.get(self._tag_key(norm_key(code))) if text(code) else None
        return None if s is None or s.is_deleted else s

    def _variant(self, code):
        v = self._variants.get(text(code))
        if v is None or v.is_deleted or v.product.is_deleted:
            return None
        return v

    def _group(self, name, key_col='InvoNo'):
        out = defaultdict(list)
        for x in self._rows(name):
            out[norm_key(x.get(key_col))].append(x)
        return out

    def _created_at(self, model, pk, when, **more):
        """History keeps its own time: created_at (and `more`) = the source date, so 'today' never counts it."""
        if when is not None:
            model.all_objects.filter(pk=pk).update(created_at=when, **more)

    # ── run ──────────────────────────────────────────────────────────────────────────────────────────────────────────
    def run_history(self):
        self._customers = self._tag_objects('customer', Customer)
        self._suppliers = self._tag_objects('supplier', Supplier)
        self._variants = self._tag_objects('product', ProductVariant, live_filter=('product',))
        self._sales()
        self._purchases()
        self._purchase_returns()
        self._serials()
        self._reservations()
        self._cheques()

    # ── sales + their returned lines ─────────────────────────────────────────────────────────────────────────────────
    def _sale_lines(self, rows, inv_seller):
        """tb_InvoOutData rows of one invoice → (sold, returned, skipped). A line of an item that was not imported is
        skipped (counted), never guessed."""
        sold, back = [], []
        for x in rows:
            try:
                v = self._variant(x.get('ItemCode'))
                if v is None:
                    raise _Skip('item not imported (ItemCode has no product)')
                qty = self._num(x, 'Qty')
                price = self._num(x, 'Price')
                if qty is None or qty == 0:
                    raise _Skip('quantity empty or 0')
                if price is None:
                    raise _Skip('price empty')
                before = self._num(x, 'PriceBefore')
                cost = self._num(x, 'Cost')
            except _Skip as exc:
                self.history_facts.note(f'sale line skipped — {exc}')
                continue
            q = abs(qty)
            line_total = _money(price * q)
            if truthy(x.get('Back')) or qty < 0:
                back.append({'variant': v, 'qty': q, 'amount': abs(line_total)})
                continue
            unit = _money(before) if before is not None and before >= price else _money(price)
            if price < 0:
                self.history_facts.note('sale line with a negative price — kept as it is')
            if cost is not None and cost > 0:
                cost_at = _money(cost)
            else:
                cost_at = _money(v.cost_price)
                self.history_facts.note('sale line without Cost — cost = the product\'s cost today')
            seller = text(x.get('UserPayNameSale'))[:150] or inv_seller
            sold.append({'variant': v, 'qty': q, 'unit_price': unit, 'discount': _money(unit * q) - line_total,
                         'total': line_total, 'cost': cost_at, 'seller': seller})
        return sold, back

    @staticmethod
    def _sig(lines):
        return sorted((str(l['variant'].pk), str(Decimal(l['qty']).normalize()), str(l['unit_price']),
                       str(l['discount']), str(l['cost']), l['seller']) for l in lines)

    @staticmethod
    def _item_sig(inv):
        return sorted((str(i.variant_id), str(Decimal(i.quantity).normalize()), str(_money(i.unit_price)),
                       str(_money(i.discount_amount)), str(_money(i.cost_at_sale)), i.salesperson_name)
                      for i in inv.items.all())

    def _sales(self):
        sec, rsec = self.report.section('sales'), self.report.section('sale_returns')
        facts = self.history_facts
        headers = self._rows('tb_InvoOut')
        if not headers:
            return
        lines = self._group('tb_InvoOutData')
        sales = self._tag_objects('sale', SalesInvoice)
        returns = self._tag_objects('sale_return', RefundInvoice)
        facts.line('tb_InvoOut.InvoType: ' + ', '.join(
            f'"{v}" × {n}' for v, n in Counter(text(x.get('InvoType')) for x in headers).most_common()))
        # Numbering (see the doc): Proline's InvoNo when the shop has no sale with it; the sequence moves past the top.
        seq = InvoiceSequence.objects.select_for_update().get_or_create(store=self.store)[0]
        used = set(SalesInvoice.all_objects.filter(store=self.store, invoice_number__isnull=False)
                   .values_list('invoice_number', flat=True))
        top = max([int(norm_key(x.get('InvoNo'))) for x in headers if norm_key(x.get('InvoNo')).isdigit()] or [0])
        if self.number_mode == 'proline' and top > seq.last_number:
            facts.line(f'sale numbers: Proline\'s own (up to {top}); the shop\'s next new sale gets {top + 1}')
            seq.last_number = top
        elif self.number_mode == 'proline':
            facts.line(f'sale numbers: Proline\'s own; the shop\'s next new sale keeps {seq.last_number + 1}')
        else:
            facts.line('sale numbers: new Vendorya numbers (--invoice-numbers new); Proline\'s stay as the old no.')
        seen = set()

        def order(x):
            return (text(x.get('Date')), int(norm_key(x.get('InvoNo'))) if norm_key(x.get('InvoNo')).isdigit() else 0)

        for x in sorted(headers, key=order):
            no = norm_key(x.get('InvoNo'))
            sample = f'#{no} · {text(x.get("Date"))[:10]}'
            if not no:
                sec.skip('empty InvoNo', sample)
                continue
            if no in seen:
                sec.skip('InvoNo appears twice in tb_InvoOut', sample)
                continue
            seen.add(no)
            rows = lines.get(no, [])
            if not rows:
                sec.skip('no lines in tb_InvoOutData', sample)
                continue
            with self.row(sec, sample):
                try:
                    when = self._when(x.get('Date'))
                    if when is None:
                        raise _Skip('no date')
                    total = self._num(x, 'Total') or ZERO
                    code, credit = text(x.get('ComCode')), text(x.get('InvoType')) in CREDIT_SALE_TYPES
                    if not code:
                        customer = self.walk_in
                    else:
                        customer, how = self._customer(code)
                        if how == 'deleted':
                            raise _Skip('customer deleted in Vendorya')
                        if customer is None and credit:
                            raise _Skip('credit sale to a customer that was not imported')
                        if customer is None:
                            customer = self.walk_in
                            facts.note('cash sale to an unknown customer code → walk-in')
                    seller = (text(x.get('UserPayName')) or text(x.get('UserAdded')))[:150]
                    sold, back = self._sale_lines(rows, seller)
                    if len(sold) + len(back) < len(rows):
                        facts.note('invoices imported WITHOUT some of their lines (see the line reasons)')
                    if not sold and not back:
                        raise _Skip('none of its lines could be imported')
                except _Skip as exc:
                    sec.skip(str(exc), sample)
                    continue
                if (self._num_safe(x, 'Payed') or ZERO) < total:
                    facts.note('sales not fully paid in Proline at the time (credit) — imported settled')
                subtotal = sum((l['total'] for l in sold), ZERO)
                extra = subtotal - sum((l['amount'] for l in back), ZERO) - total
                discount = extra if ZERO < extra <= subtotal else ZERO
                if extra < 0:
                    facts.note('header Total above the sum of its lines — the lines are kept')
                if sold:
                    self._upsert_sale(sec, sales.get(no), no, x, when, customer, seller, sold, subtotal, discount,
                                      seq, used, sample)
                elif no in sales:
                    sec.skip('Proline now has only returned lines on this invoice — the sale is left alone', sample)
                else:
                    facts.note('invoices with only returned lines (a return, no sale)')
                if back:
                    self._upsert_sale_return(rsec, returns.get(no), no, when, customer, back, sample)
        seq.save(update_fields=['last_number'])

    @staticmethod
    def _num_safe(x, col):
        try:
            return parse_decimal(x.get(col), col)
        except BadNumber:
            return None

    def _upsert_sale(self, sec, inv, no, x, when, customer, seller, sold, subtotal, discount, seq, used, sample):
        grand = subtotal - discount
        values = {'customer': customer, 'date': when, 'subtotal': subtotal, 'discount': discount,
                  'grand_total': grand, 'paid_amount': grand, 'salesperson_name': seller}
        if inv is not None:
            ref = self.refs.get('sale', no)
            self.refs.touch(ref)
            if inv.is_deleted or inv.status != SalesInvoice.Status.POSTED:
                sec.skip('voided / deleted in Vendorya — left alone', sample)
                return
            changes = [f for f, v in values.items()
                       if (inv.customer_id != v.pk if f == 'customer' else getattr(inv, f) != v)]
            lines_changed = self._item_sig(inv) != self._sig(sold)
            if not changes and not lines_changed:
                sec.unchanged()
                return
            if inv.refunds.exists() or inv.payments.exists():
                sec.skip('changed in Proline, but it has returns / payments in Vendorya — left alone', sample)
                return
            for f, v in values.items():
                setattr(inv, f, v)
            inv.save()
            if lines_changed:
                inv.items.all().delete()       # lines the import made — replaced by the fresher export's
                self._sale_items(inv, sold)
                changes.append('lines')
            self._created_at(SalesInvoice, inv.pk, when)
            sec.update(f'{sample}: {", ".join(changes)}')
            return
        number = int(no) if no.isdigit() and len(no) <= 9 else None
        if self.number_mode != 'proline' or number is None or number in used or number == 0:
            if self.number_mode == 'proline':
                self.history_facts.note('sale number already used in this shop — took the next free number')
            seq.last_number += 1
            number = seq.last_number
        used.add(number)
        inv = SalesInvoice(store=self.store, branch=self.branch, status=SalesInvoice.Status.POSTED,
                           invoice_number=number, tax_total=ZERO, source_number=no[:30], **values)
        inv.save()
        self._sale_items(inv, sold)
        self._created_at(SalesInvoice, inv.pk, when)
        self.refs.tag('sale', no, inv, CREATED)
        sec.add(f'{sample} → #{number}')

    @staticmethod
    def _sale_items(inv, sold):
        # bulk_create: no save() → no salesperson default from a request, nothing else fires (total computed here
        # exactly as SalesInvoiceItem.save would: qty × unit price − discount).
        SalesInvoiceItem.objects.bulk_create([SalesInvoiceItem(
            invoice=inv, variant=l['variant'], quantity=l['qty'], unit_price=l['unit_price'],
            discount_amount=l['discount'], tax_amount=ZERO, total=l['total'], cost_at_sale=l['cost'],
            salesperson_name=l['seller']) for l in sold])

    def _upsert_sale_return(self, sec, ret, no, when, customer, back, sample):
        total = sum((l['amount'] for l in back), ZERO)
        if ret is not None:
            self.refs.touch(self.refs.get('sale_return', no))
            if ret.is_deleted:
                sec.skip('deleted in Vendorya — left alone', sample)
                return
            have = sorted((str(i.variant_id), str(Decimal(i.quantity).normalize()), str(_money(i.refund_amount)))
                          for i in ret.items.all())
            want = sorted((str(l['variant'].pk), str(Decimal(l['qty']).normalize()), str(l['amount'])) for l in back)
            if have == want and ret.customer_id == customer.pk:
                sec.unchanged()
                return
            ret.items.all().delete()
            self._refund_items(ret, back)
            RefundInvoice.all_objects.filter(pk=ret.pk).update(customer=customer, total_refunded=total, paid_back=total,
                                                               date=when, created_at=when)
            sec.update(f'{sample}: lines')
            return
        # A free return (no original sale — Proline does not say which sale it was), paid back in full in Proline:
        # no balance moves, no drawer cash, no shift, no store credit. Lines never restock (bulk, restock off).
        ret = RefundInvoice(store=self.store, branch=self.branch, customer=customer, original_invoice=None,
                            refund_method=RefundInvoice.RefundMethod.CASH, total_refunded=total, paid_back=total,
                            drawer_cash=ZERO, reason=f'Returned in the old system (invoice {no})'[:500],
                            source_number=no[:30])
        ret.save()
        self._refund_items(ret, back)
        RefundInvoice.all_objects.filter(pk=ret.pk).update(date=when, created_at=when)   # date is auto_now_add
        self.refs.tag('sale_return', no, ret, CREATED)
        sec.add(f'{sample} → return #{ret.refund_number}')

    @staticmethod
    def _refund_items(ret, back):
        RefundItem.objects.bulk_create([RefundItem(refund=ret, variant=l['variant'], quantity=l['qty'],
                                                   refund_amount=l['amount'], restock_inventory=False)
                                        for l in back])

    # ── purchases ────────────────────────────────────────────────────────────────────────────────────────────────────
    def _buy_lines(self, rows, what):
        out = []
        for x in rows:
            try:
                v = self._variant(x.get('ItemCode'))
                if v is None:
                    raise _Skip('item not imported (ItemCode has no product)')
                qty, price = self._num(x, 'Qty'), self._num(x, 'Price')
                if qty is None or qty <= 0:
                    raise _Skip('quantity empty, 0 or below')
                if price is None or price < 0:
                    raise _Skip('price empty or below 0')
            except _Skip as exc:
                self.history_facts.note(f'{what} line skipped — {exc}')
                continue
            line_total = self._num_safe(x, 'Total')
            unit = _money(price)
            if line_total is not None and _money(line_total) != _money(qty * unit):
                self.history_facts.note(f'{what} line Total ≠ Qty × Price — Qty × Price kept')
            out.append({'variant': v, 'qty': qty, 'unit_cost': unit, 'total': _money(qty * unit)})
        return out

    def _purchases(self):
        sec = self.report.section('purchases')
        headers = self._rows('tb_InvoIn')
        if not headers:
            return
        lines = self._group('tb_InvoInData')
        made = self._tag_objects('purchase', PurchaseInvoice)
        seen = set()
        for x in headers:
            no = norm_key(x.get('InvoNo'))
            sample = f'#{no} · {text(x.get("Date"))[:10]}'
            if not no or no in seen:
                sec.skip('empty or repeated InvoNo', sample)
                continue
            seen.add(no)
            with self.row(sec, sample):
                try:
                    when = self._when(x.get('Date'))
                    if when is None:
                        raise _Skip('no date')
                    rows = lines.get(no, [])
                    if not rows:
                        raise _Skip('no lines in tb_InvoInData')
                    items = self._buy_lines(rows, 'purchase')
                    if not items:
                        raise _Skip('none of its lines could be imported')
                except _Skip as exc:
                    sec.skip(str(exc), sample)
                    continue
                supplier = self._supplier(x.get('ComCode'))
                if supplier is None:
                    code = norm_key(x.get('ComCode'))
                    self.history_facts.note(
                        'purchases from a party imported as a CUSTOMER only (tb_Cust type says customer) — imported '
                        'without a supplier' if code and self._customers.get(self._tag_key(code)) is not None
                        else 'purchases with no / unknown supplier — imported without one')
                if (self._num_safe(x, 'Dis') or ZERO) != 0:
                    self.history_facts.note('purchase header discount (Dis) — no field for it, lines kept at their price')
                total = sum((l['total'] for l in items), ZERO)
                inv = made.get(no)
                if inv is not None:
                    self.refs.touch(self.refs.get('purchase', no))
                    if inv.is_deleted:
                        sec.skip('deleted in Vendorya — left alone', sample)
                        continue
                    have = sorted((str(i.variant_id), str(Decimal(i.quantity).normalize()), str(_money(i.unit_cost)))
                                  for i in inv.items.all())
                    want = sorted((str(l['variant'].pk), str(Decimal(l['qty']).normalize()), str(l['unit_cost']))
                                  for l in items)
                    if have == want and inv.supplier_id == getattr(supplier, 'pk', None) and inv.date == when:
                        sec.unchanged()
                        continue
                    if have != want:
                        inv.items.all().delete()
                        self._purchase_items(inv, items)
                    PurchaseInvoice.all_objects.filter(pk=inv.pk).update(
                        supplier=supplier, date=when, total_amount=total, paid_amount=total, created_at=when)
                    sec.update(f'{sample}: ' + ('lines' if have != want else 'supplier / date'))
                    continue
                inv = PurchaseInvoice(store=self.store, branch=self.branch, supplier=supplier, date=when,
                                      status=PurchaseInvoice.Status.RECEIVED, notes=text(x.get('Note')),
                                      total_amount=total, paid_amount=total, source_number=no[:30])
                inv.save()                 # born RECEIVED: the receive signal never runs (no stock, no cost change)
                self._purchase_items(inv, items)
                self._created_at(PurchaseInvoice, inv.pk, when)
                self.refs.tag('purchase', no, inv, CREATED)
                sec.add(f'{sample} → {inv.purchase_number or "no supplier"}')

    @staticmethod
    def _purchase_items(inv, items):
        # bulk_create: PurchaseItem.save would re-save the invoice once per line (and nothing else is wanted here).
        PurchaseItem.objects.bulk_create([PurchaseItem(invoice=inv, variant=l['variant'], quantity=l['qty'],
                                                       unit_cost=l['unit_cost'], total_cost=l['total'])
                                          for l in items])

    # ── returns to suppliers ─────────────────────────────────────────────────────────────────────────────────────────
    def _purchase_returns(self):
        from finance.purchase_returns import _next_number
        sec = self.report.section('purchase_returns')
        headers = self._rows('tb_InvoInBack')
        if not headers:
            return
        lines = self._group('tb_InvoInDataBack')
        made = self._tag_objects('purchase_return', PurchaseReturn)
        per_no = Counter(norm_key(x.get('InvoNo')) for x in headers)
        seen = set()
        for x in headers:
            no, day = norm_key(x.get('InvoNo')), text(x.get('Date'))[:10]
            key, sample = f'{no}@{day}', f'#{no} · {day}'
            if not no or key in seen:
                sec.skip('empty InvoNo, or the same number twice on one day', sample)
                continue
            seen.add(key)
            with self.row(sec, sample):
                try:
                    when = self._when(x.get('Date'))
                    if when is None:
                        raise _Skip('no date')
                    rows = lines.get(no, [])
                    if per_no[no] > 1:      # Proline reused the number: its lines are told apart by their date
                        rows = [r for r in rows if text(r.get('Date'))[:10] == day]
                    if not rows:
                        raise _Skip('no lines in tb_InvoInDataBack')
                    supplier = self._supplier(x.get('ComCode'))
                    if supplier is None:
                        code = norm_key(x.get('ComCode'))
                        raise _Skip('the party is imported as a CUSTOMER only (tb_Cust type says customer) — a '
                                    'return needs a supplier' if code and self._customers.get(self._tag_key(code))
                                    is not None else 'supplier not imported')
                    items = self._buy_lines(rows, 'supplier return')
                    if not items:
                        raise _Skip('none of its lines could be imported')
                except _Skip as exc:
                    sec.skip(str(exc), sample)
                    continue
                total = sum((l['total'] for l in items), ZERO)
                ret = made.get(key)
                if ret is not None:
                    self.refs.touch(self.refs.get('purchase_return', key))
                    have = sorted((str(i.variant_id), str(Decimal(i.quantity).normalize()), str(_money(i.unit_cost)))
                                  for i in ret.items.all())
                    want = sorted((str(l['variant'].pk), str(Decimal(l['qty']).normalize()), str(l['unit_cost']))
                                  for l in items)
                    if ret.is_deleted or ret.status != PurchaseReturn.Status.POSTED:
                        sec.skip('voided / deleted in Vendorya — left alone', sample)
                    elif have == want and ret.supplier_id == supplier.pk:
                        sec.unchanged()
                    else:
                        ret.items.all().delete()
                        self._return_items(ret, items)
                        PurchaseReturn.all_objects.filter(pk=ret.pk).update(supplier=supplier, total_amount=total)
                        sec.update(f'{sample}: lines / supplier')
                    continue
                # Posted as it stands: no stock leaves (lines bulk-created), no money (refund 0), out of the account.
                ret = PurchaseReturn.all_objects.create(
                    store=self.store, branch=self.branch, supplier=supplier, purchase=None,
                    return_number=_next_number(self.store), date=when, status=PurchaseReturn.Status.POSTED,
                    total_amount=total, refund_amount=ZERO, notes=text(x.get('Note')), source_number=no[:30])
                self._return_items(ret, items)
                self._created_at(PurchaseReturn, ret.pk, when)
                self.refs.tag('purchase_return', key, ret, CREATED)
                sec.add(f'{sample} → return #{ret.return_number}')

    @staticmethod
    def _return_items(ret, items):
        PurchaseReturnItem.objects.bulk_create([PurchaseReturnItem(
            purchase_return=ret, variant=l['variant'], quantity=l['qty'], unit_cost=l['unit_cost'],
            total_cost=l['total']) for l in items])

    # ── serials: counted only ────────────────────────────────────────────────────────────────────────────────────────
    def _serials(self):
        """No serial units are made (the shop: every physical device has its OWN item code — the code is the
        identifier; the lines' Serial cell says "NO"). tb_Serial is the repair desk's device log (repairs come later).
        Both are only counted."""
        facts = self.history_facts
        repair = self._rows('tb_Serial')
        if repair:
            facts.note('tb_Serial rows = the repair desk\'s device in / out log — NOT imported (repairs come later)',
                       len(repair))
            facts.line('tb_Serial.InvoType: ' + ', '.join(
                f'"{v}" × {n}' for v, n in Counter(text(x.get('InvoType')) for x in repair).most_common(10)))
        for table in ('tb_InvoInData', 'tb_InvoInDataBack', 'tb_InvoOutData'):
            seen = Counter(text(x.get('Serial')) or '(empty)' for x in self._rows(table))
            if seen:
                facts.line(f'{table}.Serial values (no serial units are made): ' + ', '.join(
                    f'"{v}" × {n}' for v, n in seen.most_common(3)) + (f' … {len(seen)} different' if len(seen) > 3
                                                                       else ''))

    # ── reservations: counted only ───────────────────────────────────────────────────────────────────────────────────
    def _reservations(self):
        """The shop: StoreState "out" = the reservation became a sales invoice (that sale is in tb_InvoOut; Remaining is
        never updated after it). So none is imported; any other state = an open reservation — not imported yet."""
        orders = self._rows('tb_Reservation_Order')
        if not orders:
            return
        facts = self.history_facts
        facts.line('tb_Reservation_Order.StoreState: ' + ', '.join(
            f'"{v}" × {n}' for v, n in Counter(text(x.get('StoreState')) for x in orders).most_common()))
        for x in orders:
            if text(x.get('StoreState')).casefold() == 'out':
                facts.note('reservations already converted to sales (StoreState out — their sale is in tb_InvoOut) '
                           '— not imported')
            else:
                facts.note(f'OPEN reservations (StoreState "{text(x.get("StoreState"))}") — open reservations are not '
                           f'imported yet: enter them by hand')

    # ── tb_BankChecks: counted only ──────────────────────────────────────────────────────────────────────────────────
    def _cheques(self):
        """tb_BankChecks is NOT cheques (the shop): numbers and Check_Status are typed at random so Proline accepts the
        row; it is only a reminder of which CUSTOMER owes how much and when (customer + amount + DateDue + note). It
        maps to a future "payment due dates on a customer" feature — counted here, never imported."""
        rows = self._rows('tb_BankChecks')
        if not rows:
            return
        facts, today = self.history_facts, timezone.localdate().isoformat()
        when = defaultdict(lambda: [0, ZERO])
        for x in rows:
            due = text(x.get('DateDue'))[:10]
            key = 'due today or later' if due and due >= today else 'due date passed' if due else 'no due date'
            when[key][0] += 1
            when[key][1] += self._num_safe(x, 'Amount') or ZERO
        total = sum((v[1] for v in when.values()), ZERO)
        facts.line(f'tb_BankChecks (payment due dates on customers — a future feature) — NOT imported: {len(rows)} '
                   f'rows, amount {_money(total)} · ' + ' · '.join(
                       f'{k}: {n} rows, {_money(a)}' for k, (n, a) in sorted(when.items())))
        facts.line('tb_BankChecks.AccType: ' + ', '.join(
            f'"{v}" × {n}' for v, n in Counter(text(x.get('AccType')) for x in rows).most_common()))
