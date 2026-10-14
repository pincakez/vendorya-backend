"""Owner "export my shop's data" zip (s167) — core/shop_export.py."""
import csv
import io
import zipfile
from decimal import Decimal

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import ActivityLog, Address, Branch, Store
from core.shop_export import PRODUCTS_FILE, TABLES, _cell
from finance.models import Expense, ExpenseCategory, PaymentMethod, Payment, SalesInvoice, SalesInvoiceItem
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
        self.assertRegex(row[head.index('date')], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')

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
