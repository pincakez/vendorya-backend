"""Owner "export my shop's data" zip (s167) — core/shop_export.py."""
import csv
import io
import zipfile
from decimal import Decimal

from django.conf import settings as dj_settings
from django.core.cache import cache
from unittest import mock

from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import ActivityLog, Address, Branch, Store
from core.shop_export import PRODUCTS_FILE, TABLES, _cell
from finance.models import (CustomerReceipt, Expense, ExpenseCategory, PaymentMethod, Payment, PurchaseInvoice,
                            PurchaseItem, RefundInvoice, RefundItem, SalesInvoice, SalesInvoiceItem, SupplierPayment)
from inventory.models import Product, ProductVariant, Supplier
from users.models import Customer, User

URL = '/api/core/shop-export/'
EXPECTED_FILES = {PRODUCTS_FILE, 'README.txt'} | {name for name, *_ in TABLES}


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


def _shop(name, code, prefix):
    owner = User.objects.create_user(username=f'{prefix}_own', password='pw-123456789', role='OWNER')
    store = Store.objects.create(name=name, store_code=code, owner=owner)
    owner.store = store
    owner.save(update_fields=['store'])
    branch = Branch.all_objects.filter(store=store).first() or Branch.objects.create(
        store=store, name='Main', is_main_branch=True,
        address=Address.objects.create(store=store, street_1='1', city='Cairo'))
    return store, owner, branch


def _sale(store, branch, customer, number, variant, qty=Decimal('2'), price=Decimal('50.00')):
    total = qty * price
    inv = SalesInvoice.objects.create(
        store=store, branch=branch, customer=customer, invoice_number=number,
        status=SalesInvoice.Status.POSTED, date=timezone.now(), subtotal=total, grand_total=total,
        paid_amount=total)
    SalesInvoiceItem.objects.create(invoice=inv, variant=variant, quantity=qty, unit_price=price, total=total)
    method = PaymentMethod.all_objects.filter(store=store).first() or PaymentMethod.objects.create(
        store=store, name='Cash', is_cash=True)
    Payment.objects.create(invoice=inv, method=method, amount=total)
    return inv


class ShopExportTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        cache.clear()   # the 1-per-minute throttle lives in the cache
        self.store, self.owner, self.branch = _shop('Acme', 'ACM', 'a')
        self.cashier = User.objects.create_user(username='a_csh', password='pw-123456789', role='CASHIER', store=self.store)
        self.manager = User.objects.create_user(username='a_mgr', password='pw-123456789', role='MANAGER', store=self.store)
        self.admin = User.objects.create_user(username='a_adm', password='pw-123456789', role='ADMIN', store=self.store)

        self.supplier = Supplier.objects.create(store=self.store, name='Nile Supply')
        prod = Product.objects.create(store=self.store, name='Laptop X', supplier=self.supplier)
        self.variant = ProductVariant.objects.create(product=prod, store=self.store, sku='A-1',
                                                     cost_price=Decimal('30.00'), sell_price=Decimal('50.00'))
        self.arabic = Customer.objects.create(store=self.store, name='محمد أحمد', phone_number='+20 100 123 4567')
        self.gone = Customer.objects.create(store=self.store, name='Deleted Dan')
        self.gone.delete()   # soft delete
        self.sale = _sale(self.store, self.branch, self.arabic, 1001, self.variant)
        cat = ExpenseCategory.objects.create(store=self.store, name='Rent')
        Expense.objects.create(store=self.store, branch=self.branch, category=cat, amount=Decimal('1200.50'),
                               description='October rent', date=timezone.localdate())
        bank = PaymentMethod.objects.create(store=self.store, name='Bank Misr')
        SupplierPayment.objects.create(store=self.store, supplier=self.supplier, amount=Decimal('75.25'),
                                       method='BANK', payment_method=bank)
        # A purchase with one line, a refund with one line and a customer receipt — every CSV gets a row.
        self.purchase = PurchaseInvoice.objects.create(
            store=self.store, branch=self.branch, supplier=self.supplier, purchase_number='P-77',
            vendor_reference='NS-9', date=timezone.now(), status=PurchaseInvoice.Status.RECEIVED, total_amount=Decimal('90.00'))
        PurchaseItem.objects.create(invoice=self.purchase, variant=self.variant, quantity=Decimal('3'),
                                    unit_cost=Decimal('30.00'), total_cost=Decimal('90.00'), batch_number='B1')
        self.refund = RefundInvoice.objects.create(
            store=self.store, branch=self.branch, original_invoice=self.sale, customer=self.arabic,
            refund_number=501, date=timezone.now(), total_refunded=Decimal('50.00'), paid_back=Decimal('50.00'), reason='Broken')
        RefundItem.objects.create(refund=self.refund, variant=self.variant, quantity=Decimal('1'),
                                  refund_amount=Decimal('50.00'), restock_inventory=False)   # True would move stock in save()
        self.receipt = CustomerReceipt.objects.create(
            store=self.store, customer=self.arabic, amount=Decimal('20.00'), method=bank,
            unallocated=Decimal('20.00'), note='Deposit', created_by=self.owner)

        # Another shop with its own customer + sale — must never appear.
        self.other, _o_owner, o_branch = _shop('Other', 'OTH', 'b')
        o_prod = Product.objects.create(store=self.other, name='Other Phone')
        o_var = ProductVariant.objects.create(product=o_prod, store=self.other, sku='B-1',
                                              cost_price=Decimal('1'), sell_price=Decimal('2'))
        self.o_cust = Customer.objects.create(store=self.other, name='Secret Sam')
        self.o_sale = _sale(self.other, o_branch, self.o_cust, 777, o_var)

    def _download(self, user):
        r = _client(user).get(URL)
        self.assertEqual(r.status_code, 200, getattr(r, 'content', b'')[:300])
        body = b''.join(r.streaming_content)
        return r, zipfile.ZipFile(io.BytesIO(body))

    @staticmethod
    def _rows(zf, name):
        raw = zf.read(name)
        return raw, list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))

    def test_owner_gets_zip_with_every_file_and_header(self):
        r, zf = self._download(self.owner)
        self.assertEqual(r['Content-Type'], 'application/zip')
        self.assertIn('vendorya-ACM-export-', r['Content-Disposition'])
        self.assertEqual(set(zf.namelist()), EXPECTED_FILES)
        for name, _desc, cols, _rows in TABLES:
            raw, rows = self._rows(zf, name)
            self.assertTrue(raw.startswith(b'\xef\xbb\xbf'), f'{name} has no BOM')
            self.assertEqual(rows[0], [c for c, _ in cols], name)
        raw, rows = self._rows(zf, PRODUCTS_FILE)
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        self.assertIn('SKU', rows[0])
        self.assertIn('A-1', [c for row in rows for c in row])
        readme = zf.read('README.txt').decode('utf-8')
        for name in EXPECTED_FILES - {'README.txt'}:
            self.assertIn(name, readme)

    def test_rows_join_and_money_is_plain(self):
        _r, zf = self._download(self.owner)
        _raw, sales = self._rows(zf, 'sales.csv')
        head, row = sales[0], sales[1]
        self.assertEqual(len(sales), 2)
        self.assertEqual(row[head.index('invoice_number')], '1001')
        self.assertEqual(row[head.index('grand_total')], '100.00')
        self.assertEqual(row[head.index('still_owed')], '0.00')
        inv_id = row[head.index('invoice_id')]
        _raw, lines = self._rows(zf, 'sales_lines.csv')
        self.assertEqual(lines[1][lines[0].index('invoice_id')], inv_id)
        self.assertEqual(lines[1][lines[0].index('sku')], 'A-1')
        _raw, pays = self._rows(zf, 'sales_payments.csv')
        self.assertEqual(pays[1][pays[0].index('invoice_id')], inv_id)
        _raw, exp = self._rows(zf, 'expenses.csv')
        self.assertEqual(exp[1][exp[0].index('amount')], '1200.50')
        self.assertRegex(exp[1][exp[0].index('date')], r'^\d{4}-\d{2}-\d{2}$')
        _raw, sp = self._rows(zf, 'supplier_payments.csv')
        self.assertEqual(sp[1][sp[0].index('method')], 'Bank Misr')
        self.assertEqual(sp[1][sp[0].index('amount')], '75.25')
        self.assertRegex(row[head.index('date')], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')
        # the 50 refunded + 50 paid back leaves the paid sale at 0 owed (invoice_outstanding)
        self.assertEqual(row[head.index('returned')], '50.00')

    def _dicts(self, zf, name):
        _raw, rows = self._rows(zf, name)
        return [dict(zip(rows[0], r)) for r in rows[1:]]

    def test_purchase_refund_receipt_rows(self):
        _r, zf = self._download(self.owner)
        [p] = self._dicts(zf, 'purchases.csv')
        self.assertEqual((p['purchase_id'], p['purchase_number'], p['supplier_reference'], p['status'],
                          p['supplier_id'], p['supplier_name'], p['total_amount'], p['branch']),
                         (str(self.purchase.id), 'P-77', 'NS-9', 'RECEIVED', str(self.supplier.id),
                          'Nile Supply', '90.00', self.branch.name))
        [pl] = self._dicts(zf, 'purchase_lines.csv')
        self.assertEqual((pl['purchase_id'], pl['purchase_number'], pl['sku'], pl['product_name'], pl['quantity'],
                          pl['unit_cost'], pl['line_total'], pl['batch_number'], pl['unit'], pl['expiry_date']),
                         (str(self.purchase.id), 'P-77', 'A-1', 'Laptop X', '3.000', '30.00', '90.00', 'B1', '', ''))
        [rf] = self._dicts(zf, 'refunds.csv')
        self.assertEqual((rf['refund_id'], rf['refund_number'], rf['original_invoice_id'],
                          rf['original_invoice_number'], rf['customer_name'], rf['total_refunded'],
                          rf['paid_back'], rf['refund_method'], rf['reason']),
                         (str(self.refund.id), '501', str(self.sale.id), '1001', 'محمد أحمد', '50.00', '50.00',
                          'CASH', 'Broken'))
        [rl] = self._dicts(zf, 'refund_lines.csv')
        self.assertEqual((rl['refund_id'], rl['refund_number'], rl['sku'], rl['quantity'], rl['refund_amount'],
                          rl['back_to_stock'], rl['unit_factor']),
                         (str(self.refund.id), '501', 'A-1', '1.000', '50.00', 'false', '1.000'))
        [rc] = self._dicts(zf, 'customer_receipts.csv')
        self.assertEqual((rc['receipt_id'], rc['customer_id'], rc['method'], rc['amount'], rc['unallocated'],
                          rc['advance'], rc['note'], rc['recorded_by']),
                         (str(self.receipt.id), str(self.arabic.id), 'Bank Misr', '20.00', '20.00', '0.00',
                          'Deposit', 'a_own'))
        [sp] = self._dicts(zf, 'supplier_payments.csv')
        self.assertEqual(sp['supplier_id'], str(self.supplier.id))
        # the customer's balance = with_live_balance (0 owed on the sale − 20 unallocated receipt)
        cust = {c['name']: c for c in self._dicts(zf, 'customers.csv')}
        self.assertEqual(cust['محمد أحمد']['balance'], '-20.00')

    def test_arabic_survives_and_deleted_excluded(self):
        _r, zf = self._download(self.owner)
        raw, rows = self._rows(zf, 'customers.csv')
        names = [row[rows[0].index('name')] for row in rows[1:]]
        self.assertIn('محمد أحمد', names)
        self.assertIn('محمد أحمد'.encode('utf-8'), raw)
        self.assertNotIn('Deleted Dan', names)
        phones = [row[rows[0].index('phone')] for row in rows[1:]]
        self.assertIn('+20 100 123 4567', phones)   # a phone is not mangled by the formula guard

    def test_other_shop_never_appears(self):
        _r, zf = self._download(self.owner)
        everything = b''.join(zf.read(n) for n in zf.namelist()).decode('utf-8-sig', errors='replace')
        self.assertNotIn('Secret Sam', everything)
        self.assertNotIn(str(self.o_sale.id), everything)
        self.assertNotIn(str(self.o_cust.id), everything)
        self.assertNotIn('Other Phone', everything)
        self.assertNotIn('b_own', everything)

    def test_staff_has_no_secrets(self):
        _r, zf = self._download(self.owner)
        _raw, rows = self._rows(zf, 'staff.csv')
        self.assertEqual(rows[0], ['username', 'first_name', 'last_name', 'role'])
        self.assertEqual({r[0] for r in rows[1:]}, {'a_own', 'a_csh', 'a_mgr', 'a_adm'})
        blob = b''.join(zf.read(n) for n in zf.namelist())
        self.assertNotIn(self.owner.password.encode(), blob)
        self.assertNotIn(b'pbkdf2', blob)

    def test_logged_in_activity_log(self):
        self._download(self.owner)
        log = ActivityLog.all_objects.filter(store=self.store, user=self.owner).order_by('-timestamp').first()
        self.assertIsNotNone(log)
        self.assertIn('Exported', log.action)

    def test_lower_roles_refused(self):
        for u in (self.cashier, self.manager, self.admin):
            r = _client(u).get(URL)
            self.assertEqual(r.status_code, 403, u.role)

    def test_sudo_refused(self):
        sudo = User.objects.create_user(username='root', password='pw-123456789', is_superadmin=True)
        r = _client(sudo).get(URL)
        self.assertEqual(r.status_code, 403)

    def test_throttled_second_time(self):
        self._download(self.owner)
        r = _client(self.owner).get(URL)
        self.assertEqual(r.status_code, 429)

    def test_formula_guard(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo('UTC')
        self.assertEqual(_cell('=HYPERLINK("x")', tz), '\'=HYPERLINK("x")')
        self.assertEqual(_cell('+20 100', tz), '+20 100')
        self.assertEqual(_cell('-1+cmd', tz), "'-1+cmd")
        self.assertEqual(_cell(Decimal('-5.00'), tz), '-5.00')


class ShopExportSnapshotTests(TransactionTestCase):
    """The whole export runs in ONE REPEATABLE READ, READ ONLY transaction (s167 review), so every CSV is
    the same picture of the shop. TransactionTestCase = no outer test transaction, like a real request."""

    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        cache.clear()
        self.store, self.owner, self.branch = _shop('Snap', 'SNP', 's')
        prod = Product.objects.create(store=self.store, name='Mouse')
        variant = ProductVariant.objects.create(product=prod, store=self.store, sku='S-1',
                                                cost_price=Decimal('1'), sell_price=Decimal('2'))
        cust = Customer.objects.create(store=self.store, name='Nour')
        _sale(self.store, self.branch, cust, 1, variant)

    def test_export_runs_in_one_read_only_repeatable_read_snapshot(self):
        from django.db import connection
        from core import shop_export
        seen = {}

        def probe(store):
            with connection.cursor() as cur:
                cur.execute('SHOW transaction_isolation')
                seen['isolation'] = cur.fetchone()[0]
                cur.execute('SHOW transaction_read_only')
                seen['read_only'] = cur.fetchone()[0]
            seen['atomic'] = connection.in_atomic_block
            return iter(())

        tables = shop_export.TABLES + [('probe.csv', '', [('x', '')], probe)]
        with mock.patch.object(shop_export, 'TABLES', tables):
            r = _client(self.owner).get(URL)
            self.assertEqual(r.status_code, 200)
            zf = zipfile.ZipFile(io.BytesIO(b''.join(r.streaming_content)))
        self.assertEqual(seen, {'isolation': 'repeatable read', 'read_only': 'on', 'atomic': True})
        # the .iterator() tables ran inside that snapshot and still produced their rows
        self.assertIn('Nour', zf.read('customers.csv').decode('utf-8-sig'))
        self.assertEqual(len(zf.read('sales_lines.csv').decode('utf-8-sig').strip().splitlines()), 2)
        # the ActivityLog row is written AFTER the read-only snapshot closed (a write inside it would fail)
        self.assertTrue(ActivityLog.all_objects.filter(store=self.store, action__startswith='Exported').exists())
        self.assertFalse(connection.in_atomic_block)
