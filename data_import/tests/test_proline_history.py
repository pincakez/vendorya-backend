"""Import from another system — the Proline adapter, part B (history), on INVENTED CSVs in Proline's exact column
layout: part A's master fixtures (`fixtures/proline/`) + the history tables (`fixtures/proline_history/`).
The rule under test: history never moves today's money or stock (PROLINE-IMPORT-B.md)."""
import csv
import os
import shutil
import tempfile
from datetime import datetime
from decimal import Decimal

from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from io import StringIO
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from core.models import StoreSettings
from data_import.adapters.proline import ProlineAdapter
from data_import.core import run_import
from data_import.models import ExternalRef
from finance.models import (Cheque, CustomerReceipt, InvoiceSequence, Payment, PurchaseInvoice, PurchaseReturn,
                            RefundInvoice, Reservation, SalesInvoice, SalesInvoiceItem,
                            SupplierPayment, WorkShift, customer_outstanding)
from finance.views import refuse_void_with_returns
from inventory.models import (ProductVariant, SerialMove, SerialUnit, StockAdjustment, StockBatch, StockLevel,
                              Supplier)
from inventory.views import supplier_account_sources
from treasury.models import MoneyMove
from users.models import Customer

from .test_proline_import import FIXTURES, make_shop

HISTORY = os.path.join(os.path.dirname(__file__), 'fixtures', 'proline_history')
MASTER = ('tb_Group1', 'tb_Group2', 'tb_Cust', 'tb_Item')


def cairo(s):
    return timezone.make_aware(datetime.fromisoformat(s))


def supplier_balances(store):
    purchases, payments, returns = supplier_account_sources(store)
    out = {}
    for s in Supplier.all_objects.filter(store=store):
        out[s.pk] = ((purchases.filter(supplier=s).aggregate(t=Sum('total_amount'))['t'] or 0)
                     - (payments.filter(supplier=s).aggregate(t=Sum('amount'))['t'] or 0)
                     - sum((r.total_amount - r.refund_amount for r in returns.filter(supplier=s)), Decimal('0')))
    return out


def money_and_stock(store):
    """Everything that is TODAY's money or stock in one shop — an import of history must leave all of it alone."""
    return {
        'stock': sorted(StockLevel.objects.filter(variant__store=store).values_list('variant_id', 'branch_id',
                                                                                   'quantity')),
        'adjustments': StockAdjustment.all_objects.filter(store=store).count(),
        'batches': StockBatch.objects.filter(variant__store=store).count(),
        'payments': Payment.all_objects.filter(invoice__store=store).count(),
        'receipts': CustomerReceipt.all_objects.filter(store=store).count(),
        'supplier_payments': SupplierPayment.all_objects.filter(store=store).count(),
        'box_lines': MoneyMove.objects.filter(box__store=store).count(),
        'customer_balances': sorted((str(c.pk), customer_outstanding(c))
                                    for c in Customer.all_objects.filter(store=store)),
        'wallets': sorted(Customer.all_objects.filter(store=store).values_list('id', 'store_credit')),
        'supplier_balances': sorted((str(k), v) for k, v in supplier_balances(store).items()),
        'costs': sorted(ProductVariant.all_objects.filter(store=store).values_list('id', 'cost_price')),
        'shifts': sorted((str(s.pk), s.cash_in(), s.cash_out()) for s in WorkShift.all_objects.filter(store=store)),
        'in_stock_serials': SerialUnit.all_objects.filter(store=store, status=SerialUnit.Status.IN_STOCK).count(),
        'tracked': ProductVariant.all_objects.filter(store=store, product__track_serials=True).count(),
    }


def history_counts(store):
    return {
        'sales': SalesInvoice.all_objects.filter(store=store).count(),
        'lines': SalesInvoiceItem.objects.filter(invoice__store=store).count(),
        'refunds': RefundInvoice.all_objects.filter(store=store).count(),
        'purchases': PurchaseInvoice.all_objects.filter(store=store).count(),
        'supplier_returns': PurchaseReturn.all_objects.filter(store=store).count(),
        'serials': SerialUnit.all_objects.filter(store=store).count(),
        'serial_moves': SerialMove.all_objects.filter(store=store).count(),
        'reservations': Reservation.all_objects.filter(store=store).count(),
        'cheques': Cheque.all_objects.filter(store=store).count(),
        'refs': ExternalRef.all_objects.filter(store=store).count(),
    }


class _Base(TestCase):
    def setUp(self):
        self.store, self.hand_variant = make_shop('100', 'Shop A')
        self.other, _ = make_shop('200', 'Shop B')
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=True, reservations_enabled=True)
        # a shift open during the import: its cash count must not move
        self.shift = WorkShift.all_objects.create(store=self.store, branch=self.store.branches.first(),
                                                  user=self.store.owner)
        # the shop already used sale number 9 (a sale typed in Vendorya before the import)
        SalesInvoice.all_objects.create(store=self.store, branch=self.store.branches.first(),
                                        customer=Customer.all_objects.get(store=self.store, is_walk_in=True),
                                        status=SalesInvoice.Status.VOID, date=timezone.now(), invoice_number=9)

    def folder(self, edits=(), extra_rows=None):
        """Master fixtures + history fixtures in one folder. `edits` = [(table, old, new)] text replacements;
        `extra_rows` = {table: [{col: value}]} appended (a 'fresher export')."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for t in MASTER:
            shutil.copy(os.path.join(FIXTURES, f'{t}.csv'), tmp)
        for f in os.listdir(HISTORY):
            shutil.copy(os.path.join(HISTORY, f), tmp)
        for table, old, new in edits:
            path = os.path.join(tmp, f'{table}.csv')
            with open(path, encoding='utf-8-sig') as fh:
                data = fh.read()
            assert old in data, old
            with open(path, 'w', encoding='utf-8-sig') as fh:
                fh.write(data.replace(old, new, 1))
        for table, rows in (extra_rows or {}).items():
            path = os.path.join(tmp, f'{table}.csv')
            with open(path, encoding='utf-8-sig', newline='') as fh:
                header = next(csv.reader(fh))
            with open(path, 'a', encoding='utf-8', newline='') as fh:
                w = csv.writer(fh)
                for r in rows:
                    w.writerow([r.get(c, '') for c in header])
        return tmp

    def run_import(self, commit, folder=None, **options):
        options.setdefault('link_by_code', True)
        return run_import(ProlineAdapter, self.store, folder or self.folder(), options, commit=commit)

    def master_first(self):
        """Commit part A alone, then snapshot today's money + stock: what the history run must leave alone."""
        self.assertTrue(self.run_import(commit=True, part='master').committed)
        return money_and_stock(self.store)

    def sale(self, no):
        ref = ExternalRef.objects.get(store=self.store, system='proline', kind='sale', external_key=no)
        return SalesInvoice.all_objects.get(pk=ref.object_id)


class DryRunTests(_Base):
    def test_dry_run_saves_nothing(self):
        before = (money_and_stock(self.store), history_counts(self.store), money_and_stock(self.other),
                  history_counts(self.other))
        report = self.run_import(commit=False)
        self.assertEqual(report.blockers, [])
        self.assertEqual((money_and_stock(self.store), history_counts(self.store), money_and_stock(self.other),
                          history_counts(self.other)), before)
        self.assertEqual(report.counts('sales')['add'], 8, report.render(True))
        text = report.render(dry_run=True)
        self.assertIn('DRY RUN', text)
        self.assertIn('Sales (tb_InvoOut + tb_InvoOutData)', text)

    def test_command_runs_both_parts_dry(self):
        before = history_counts(self.store)
        out = StringIO()
        call_command('import_external', 'proline', '--store', '100', '--dir', self.folder(), '--link-by-code',
                     stdout=out)
        self.assertEqual(history_counts(self.store), before)
        self.assertIn('Products (tb_Item)', out.getvalue())
        self.assertIn('Returns to suppliers', out.getvalue())


class CommitTests(_Base):
    def test_history_lands_and_moves_no_money_or_stock(self):
        before = self.master_first()
        report = self.run_import(commit=True)          # part A again (nothing changes) + the history
        self.assertTrue(report.committed, report.render(False))
        self.assertEqual(money_and_stock(self.store), before)

        sales = report.counts('sales')
        self.assertEqual((sales['add'], sales['skip']), (8, 2), report.render(False))
        skips = report.sections['sales'].reasons
        self.assertEqual(skips['credit sale to a customer that was not imported'], 1)    # #4
        self.assertEqual(skips['no lines in tb_InvoOutData'], 1)                         # #7

        s1 = self.sale('1')
        self.assertEqual((s1.invoice_number, s1.source_number, s1.status), (1, '1', SalesInvoice.Status.POSTED))
        self.assertEqual(s1.customer.name, 'محمد أحمد')
        self.assertEqual((s1.subtotal, s1.discount, s1.grand_total, s1.paid_amount),
                         (Decimal('18000'), Decimal('0'), Decimal('18000'), Decimal('18000')))   # credit → settled
        self.assertEqual((s1.date, s1.created_at), (cairo('2024-01-10 10:00:00'), cairo('2024-01-10 10:00:00')))
        self.assertEqual(s1.salesperson_name, 'seller1')
        self.assertIsNone(s1.salesperson_id)
        line = s1.items.get()
        self.assertEqual((line.unit_price, line.discount_amount, line.total, line.cost_at_sale, line.salesperson_name),
                         (Decimal('18500'), Decimal('500'), Decimal('18000'), Decimal('15000'), 'seller2'))
        self.assertFalse(Payment.all_objects.filter(invoice=s1).exists())
        # no customer / unknown code on a cash sale → walk-in; no Cost → the product's cost
        walk_in = Customer.all_objects.get(store=self.store, is_walk_in=True)
        self.assertEqual(self.sale('2').customer, walk_in)
        self.assertEqual(self.sale('3').customer, walk_in)
        self.assertEqual(self.sale('2').items.get().cost_at_sale, Decimal('150'))
        # a line of an item that was not imported is skipped, the rest of the invoice lands
        self.assertEqual(self.sale('8').items.count(), 1)
        self.assertEqual(self.sale('8').grand_total, Decimal('180'))
        # header Total below its lines → the gap is the whole-invoice discount
        s12 = self.sale('12')
        self.assertEqual((s12.subtotal, s12.discount, s12.grand_total), (Decimal('900'), Decimal('50'), Decimal('850')))
        # numbers: Proline's own; 9 was taken in this shop → the next free number past the top (14)
        self.assertEqual(self.sale('9').invoice_number, 15)
        self.assertEqual(self.sale('9').source_number, '9')
        self.assertEqual(InvoiceSequence.objects.get(store=self.store).last_number, 15)

        # returned lines: #5 = a sale of what was bought + a return of what came back; #6 = a return only
        self.assertEqual(self.sale('5').grand_total, Decimal('700'))
        self.assertFalse(ExternalRef.objects.filter(store=self.store, kind='sale', external_key='6').exists())
        r5 = RefundInvoice.all_objects.get(store=self.store, source_number='5')
        self.assertEqual((r5.total_refunded, r5.paid_back, r5.drawer_cash, r5.original_invoice_id, r5.shift_id),
                         (Decimal('250'), Decimal('250'), Decimal('0'), None, None))
        self.assertEqual(r5.date, cairo('2024-01-14 14:00:00'))
        self.assertEqual(RefundInvoice.all_objects.get(store=self.store, source_number='6').total_refunded,
                         Decimal('18000'))
        # #14 sold AND took back the same item → the return names that sale (balance still 0: paid back in full)
        r14 = RefundInvoice.all_objects.get(store=self.store, source_number='14')
        self.assertEqual((r14.original_invoice, r14.paid_back), (self.sale('14'), Decimal('250')))
        self.assertIsNone(r5.original_invoice)        # #5 took back an item it did not sell → a free return
        self.assertTrue(r5.items.get().restock_inventory)   # cost back in the P&L — and still no stock moved

        # purchases: born RECEIVED, settled, out of the supplier account; a missing supplier → none
        p1 = PurchaseInvoice.all_objects.get(store=self.store, source_number='1')
        self.assertEqual((p1.status, p1.total_amount, p1.paid_amount, p1.supplier.name),
                         (PurchaseInvoice.Status.RECEIVED, Decimal('30000'), Decimal('30000'), 'شركة النور للتوريدات'))
        self.assertEqual(p1.date, cairo('2023-12-01 09:00:00'))
        self.assertIsNone(PurchaseInvoice.all_objects.get(store=self.store, source_number='2').supplier)
        # supplier returns: the same Proline number on two days → two returns
        rets = PurchaseReturn.all_objects.filter(store=self.store, source_number='1').order_by('date')
        self.assertEqual([r.total_amount for r in rets], [Decimal('300'), Decimal('15000')])
        self.assertEqual({r.status for r in rets}, {PurchaseReturn.Status.POSTED})

        # the shop's next new sale continues after the imported numbers, and searching finds the old number
        api = APIClient()
        api.force_authenticate(self.store.owner)
        found = api.get('/api/finance/invoices/', {'search': '9'}).json()
        rows = found['results'] if isinstance(found, dict) else found
        self.assertIn(15, [r['invoice_number'] for r in rows])
        self.assertIn('9', [r['source_number'] for r in rows])

    def test_second_run_adds_nothing(self):
        self.run_import(commit=True)
        before = (history_counts(self.store), money_and_stock(self.store))
        report = self.run_import(commit=True)
        for key in ('sales', 'sale_returns', 'purchases', 'purchase_returns'):
            c = report.counts(key)
            self.assertEqual((c['add'], c['update']), (0, 0), (key, report.render(False)))
        self.assertEqual((history_counts(self.store), money_and_stock(self.store)), before)

    def test_fresher_export_adds_only_new_and_updates_changed(self):
        self.run_import(commit=True)
        n = history_counts(self.store)
        new_sale = {'InvoNo': '15', 'Date': '2024-04-01 10:00:00', 'ComCode': '1', 'InvoType': 'نقدى',
                    'Total': '250', 'Payed': '250'}
        new_line = {'InvoNo': '15', 'Date': '2024-04-01 10:00:00', 'ComCode': '1', 'ItemCode': '12600', 'Qty': '1',
                    'Price': '250', 'PriceBefore': '250', 'Total': '250', 'Back': ''}
        folder = self.folder(edits=[('tb_InvoOutData', 'بالقطعة,,,2,,,250.0000,500.0', 'بالقطعة,,,3,,,250.0000,750.0')],
                             extra_rows={'tb_InvoOut': [new_sale], 'tb_InvoOutData': [new_line]})
        report = self.run_import(commit=True, folder=folder)
        c = report.counts('sales')
        self.assertEqual((c['add'], c['update']), (1, 1), report.render(False))
        self.assertEqual(history_counts(self.store)['sales'], n['sales'] + 1)
        # Proline 15 is already Vendorya #15 (given to Proline 9 on the first run) → the next free number
        self.assertEqual(self.sale('15').invoice_number, 16)
        self.assertEqual(self.sale('2').items.get().quantity, Decimal('3'))
        self.assertEqual(self.sale('2').paid_amount, self.sale('2').grand_total)

    def test_imported_history_cannot_be_voided(self):
        self.run_import(commit=True)
        with self.assertRaises(ValidationError):
            refuse_void_with_returns(self.sale('1'))
        from finance.purchase_returns import void_return
        with self.assertRaises(ValidationError):
            void_return(PurchaseReturn.all_objects.filter(store=self.store).first(), self.store.owner, 'test')

    def test_other_shop_is_never_touched(self):
        before = (money_and_stock(self.other), history_counts(self.other))
        self.run_import(commit=True)
        self.run_import(commit=True)
        self.assertEqual((money_and_stock(self.other), history_counts(self.other)), before)

    def test_history_only_on_an_earlier_master_import(self):
        self.run_import(commit=True, part='master')
        self.assertEqual(history_counts(self.store)['sales'], 1)          # only the shop's own (void) sale
        report = self.run_import(commit=True, part='history')
        self.assertTrue(report.committed, report.render(False))
        self.assertEqual(report.counts('sales')['add'], 8)
        self.assertEqual(report.counts('products'), {k: 0 for k in ('add', 'update', 'link', 'unchanged', 'skip')})


class CountedOnlyTests(_Base):
    """Serials, the repair log and the instalment notes are counted, never imported (the shop's answers)."""

    def test_no_serial_units_and_tracking_never_switched_on(self):
        before = self.master_first()
        report = self.run_import(commit=True)
        self.assertFalse(SerialUnit.all_objects.filter(store=self.store).exists())
        self.assertFalse(SerialMove.all_objects.filter(store=self.store).exists())
        self.assertEqual(money_and_stock(self.store), before)               # incl. track_serials count
        text = report.render(dry_run=False)
        self.assertIn('10 × tb_Serial rows = the repair desk\'s device in / out log — NOT imported', text)
        self.assertIn('tb_InvoOutData.Serial values (no serial units are made): "NO" × 13', text)

    def test_instalment_notes_are_counted_never_cheques(self):
        report = self.run_import(commit=True)
        self.assertFalse(Cheque.all_objects.filter(store=self.store).exists())
        text = report.render(dry_run=False)
        self.assertIn('tb_BankChecks (payment due dates on customers — a future feature) — NOT imported: 5 rows, '
                      'amount 16900.00 · due date passed: 1 rows, 900.00 · due today or later: 4 rows, 16000.00', text)
        self.assertIn('tb_BankChecks.AccType: "ChequeInSchedual" × 3', text)


class ReservationTests(_Base):
    def test_reservations_are_counted_never_imported(self):
        before = self.master_first()
        report = self.run_import(commit=True)
        self.assertFalse(Reservation.all_objects.filter(store=self.store).exists())
        self.assertEqual(money_and_stock(self.store), before)
        text = report.render(dry_run=False)
        self.assertIn('tb_Reservation_Order.StoreState: "in" × 2, "out" × 1', text)
        self.assertIn('1 × reservations already converted to sales', text)
        self.assertIn('2 × OPEN reservations (StoreState "in") — open reservations are not imported yet', text)


class ReviewFixTests(_Base):
    """surgeon-reviewer on b33defc."""

    def test_a_returned_line_counts_against_its_imported_sale(self):
        self.run_import(commit=True)
        v = ProductVariant.objects.get(store=self.store, sku2='12600')
        api = APIClient()
        api.force_authenticate(self.store.owner)
        r = api.post('/api/finance/refunds/', {
            'branch': str(self.store.branches.first().id), 'original_invoice': str(self.sale('14').id),
            'customer': str(self.sale('14').customer_id), 'refund_method': 'CASH',
            'items': [{'variant': str(v.id), 'quantity': '1', 'refund_amount': '250'}]}, format='json')
        self.assertEqual(r.status_code, 400, r.content)        # Proline already took that unit back
        self.assertIn('refundable', str(r.content))

    def test_pnl_takes_the_returned_goods_cost_back(self):
        from datetime import date
        from reports.views import compute_pnl
        self.run_import(commit=True)
        _rows, totals = compute_pnl(self.store, date(2024, 2, 1), date(2024, 2, 29))
        # #6 (12511 at cost 15000, no purchase counts → its cost price) + #14 (12600 at the sale's cost 150)
        self.assertEqual(Decimal(str(totals['cogs_returned'])), Decimal('15150'))

    def test_a_repeated_number_skips_every_copy(self):
        folder = self.folder(extra_rows={
            'tb_InvoOut': [{'InvoNo': '2', 'Date': '2024-05-01 10:00:00', 'InvoType': 'نقدى', 'Total': '99'}],
            'tb_InvoIn': [{'InvoNo': '2', 'Date': '2024-05-01 10:00:00', 'InvoType': 'نقدى', 'Total': '99'}]})
        report = self.run_import(commit=True, folder=folder)
        self.assertEqual(report.sections['sales'].reasons[
            'InvoNo appears more than once in tb_InvoOut — its lines can\'t be told apart'], 2)
        self.assertEqual(report.sections['purchases'].reasons[
            'InvoNo appears more than once in tb_InvoIn — its lines can\'t be told apart'], 2)
        self.assertFalse(ExternalRef.objects.filter(store=self.store, kind__in=('sale', 'purchase'),
                                                    external_key='2').exists())

    def test_a_purchase_a_vendorya_return_points_at_is_never_rebuilt(self):
        from finance.models import PurchaseReturnItem
        self.run_import(commit=True)
        p1 = PurchaseInvoice.all_objects.get(store=self.store, source_number='1')
        line = p1.items.get()
        ret = PurchaseReturn.all_objects.create(store=self.store, branch=p1.branch, supplier=p1.supplier,
                                                return_number=99, total_amount=Decimal('15000'))
        PurchaseReturnItem.objects.create(purchase_return=ret, variant=line.variant, purchase_item=line,
                                          quantity=1, unit_cost=line.unit_cost, total_cost=line.unit_cost)
        folder = self.folder(edits=[('tb_InvoInData', '12511,,بالقطعة,2,15000.0000', '12511,,بالقطعة,3,15000.0000')])
        report = self.run_import(commit=True, folder=folder)
        self.assertTrue(report.committed, report.render(False))
        self.assertEqual(report.sections['purchases'].reasons[
            'lines changed in Proline, but a return to the supplier in Vendorya points at them — left alone'], 1)
        self.assertEqual(p1.items.get().pk, line.pk)

    def test_a_party_the_shop_bought_from_is_also_a_supplier(self):
        # purchase #3 now comes from party 1, whose type says customer
        folder = self.folder(edits=[('tb_InvoIn', '3,2023-12-03 09:00:00,2023-12-03 09:00:00,,1,,5,',
                                     '3,2023-12-03 09:00:00,2023-12-03 09:00:00,,1,,1,')])
        report = self.run_import(commit=True, folder=folder)
        self.assertTrue(ExternalRef.objects.filter(store=self.store, kind='supplier', external_key='ID:1').exists())
        self.assertTrue(ExternalRef.objects.filter(store=self.store, kind='customer', external_key='ID:1').exists())
        self.assertEqual(PurchaseInvoice.all_objects.get(store=self.store, source_number='3').supplier.name,
                         'محمد أحمد')
        self.assertIn('1 × type says customer, used on purchases → also supplier', report.render(dry_run=False))
        # forced --customer-codes still wins
        other = self.run_import(commit=False, folder=folder, customer_codes=['1'])
        self.assertNotIn('used on purchases → also supplier', other.render(dry_run=True))

    def test_a_dry_run_never_locks_the_sale_sequence(self):
        from unittest import mock
        with mock.patch.object(InvoiceSequence.objects, 'select_for_update',
                               side_effect=AssertionError('dry run locked the sequence')):
            report = self.run_import(commit=False)
        self.assertEqual(report.counts('sales')['add'], 8)
        self.assertFalse(InvoiceSequence.objects.filter(store=self.store).exists())
