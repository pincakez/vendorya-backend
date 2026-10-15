"""Import from another system — the Proline adapter, part A (master data), on fake CSVs in Proline's
exact column layout (`fixtures/proline/`, 5–15 rows each, with broken rows on purpose)."""
import os
import shutil
import tempfile
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import ActivityLog, Address, Branch, Store, StoreSettings
from data_import.adapters.proline import ProlineAdapter
from data_import.core import norm_key, parse_decimal, BadNumber, run_import
from data_import.models import ExternalRef
from inventory.models import Category, Product, ProductVariant, StockAdjustment, StockLevel, Supplier
from users.models import Customer, User

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'proline')


def make_shop(code, name):
    """A shop as it looks before the import: SKU2 on, a default supplier, a 'Laptop' category, one laptop
    typed by hand whose SKU2 is the old system's code (12513, with stock) and one hand-made customer."""
    owner = User.objects.create_user(username=f'own{code}', password='pw', role='OWNER')
    store = Store.objects.create(name=name, store_code=code, owner=owner)
    owner.store = store
    owner.save()
    st = StoreSettings.objects.get(store=store)
    st.sku2_state = StoreSettings.Sku2State.ON
    st.save()
    sup = Supplier.objects.create(store=store, name='Main supplier', code_prefix='400', prefix_locked=True)
    store.default_supplier = sup
    store.save(update_fields=['default_supplier'])
    branch = Branch.objects.create(store=store, name='Main', is_main_branch=True,
                                   address=Address.objects.create(store=store, street_1='1', city='Cairo'))
    laptop = Category.objects.create(store=store, name='Laptop')
    hand = Product.objects.create(store=store, name='Lenovo ThinkPad T14 (typed by hand)', supplier=sup,
                                  category=laptop)
    hv = ProductVariant.objects.create(product=hand, sku2='12513', sell_price=Decimal('23000'), barcode='12514')
    StockLevel.objects.update_or_create(variant=hv, branch=branch, defaults={'quantity': Decimal('2')})
    Customer.objects.create(store=store, name='عميل يدوي', phone_number='01099998888', notes='typed by hand')
    return store, hv


def snapshot(store):
    """Everything an import could touch in one shop, as plain values."""
    return {
        'categories': sorted(Category.all_objects.filter(store=store).values_list('id', 'name', 'parent_id')),
        'products': sorted(Product.all_objects.filter(store=store).values_list('id', 'name', 'category_id')),
        'variants': sorted(ProductVariant.all_objects.filter(store=store).values_list(
            'id', 'sku', 'sku2', 'barcode', 'cost_price', 'sell_price', 'reorder_level')),
        'customers': sorted(Customer.all_objects.filter(store=store).values_list(
            'id', 'name', 'phone_number', 'balance', 'credit_limit', 'notes')),
        'suppliers': sorted(Supplier.all_objects.filter(store=store).values_list('id', 'name', 'code_prefix')),
        'stock': sorted(StockLevel.objects.filter(variant__store=store).values_list('variant_id', 'quantity')),
        'adjustments': StockAdjustment.all_objects.filter(store=store).count(),
        'refs': ExternalRef.all_objects.filter(store=store).count(),
        'logs': ActivityLog.all_objects.filter(store=store).count(),
    }


class _Base(TestCase):
    def setUp(self):
        self.store, self.hand_variant = make_shop('100', 'Shop A')
        self.other, _ = make_shop('200', 'Shop B')

    def run_import(self, commit, folder=FIXTURES, **options):
        return run_import(ProlineAdapter, self.store, folder, options, commit=commit)

    def changed_copy(self, edit):
        """A copy of the fixtures with one cell changed — a 'fresher export'."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for f in os.listdir(FIXTURES):
            shutil.copy(os.path.join(FIXTURES, f), tmp)
        path = os.path.join(tmp, edit[0])
        with open(path, encoding='utf-8-sig') as fh:
            data = fh.read()
        assert edit[1] in data
        with open(path, 'w', encoding='utf-8-sig') as fh:
            fh.write(data.replace(edit[1], edit[2], 1))
        return tmp


class DryRunTests(_Base):
    def test_dry_run_saves_nothing_but_reports_everything(self):
        before_a, before_b = snapshot(self.store), snapshot(self.other)
        report = self.run_import(commit=False)
        self.assertEqual(snapshot(self.store), before_a)
        self.assertEqual(snapshot(self.other), before_b)
        self.assertFalse(report.committed)
        self.assertEqual(report.blockers, [])
        self.assertEqual(report.counts('products')['add'], 10)
        self.assertEqual(report.counts('products')['link'], 2)
        text = report.render(dry_run=True)
        self.assertIn('DRY RUN', text)
        self.assertIn('would add 10', text)

    def test_command_dry_run_by_default(self):
        before = snapshot(self.store)
        out = StringIO()
        call_command('import_external', 'proline', '--store', '100', '--dir', FIXTURES, stdout=out)
        self.assertEqual(snapshot(self.store), before)
        self.assertIn('DRY RUN — nothing was saved', out.getvalue())
        self.assertIn('Products (tb_Item)', out.getvalue())


class CommitTests(_Base):
    def test_commit_creates_rows(self):
        report = self.run_import(commit=True)
        self.assertTrue(report.committed, report.render(False))
        c = {k: report.counts(k) for k in ('categories', 'suppliers', 'customers', 'products')}
        self.assertEqual((c['categories']['add'], c['categories']['link'], c['categories']['skip']), (9, 1, 3))
        self.assertEqual((c['suppliers']['add'], c['suppliers']['skip']), (4, 0))
        self.assertEqual((c['customers']['add'], c['customers']['link'], c['customers']['skip']), (6, 1, 2))
        self.assertEqual((c['products']['add'], c['products']['link'], c['products']['skip']), (10, 2, 3))

        # categories: tree kept, Arabic exact, the existing "Laptop" reused, parent by name works
        dell = Category.objects.get(store=self.store, name='لابتوب ديل')
        self.assertEqual(dell.parent.name, 'لابتوب')
        self.assertEqual(Category.objects.get(store=self.store, name='ماوس').parent.name, 'اكسسوارات')
        self.assertEqual(Category.objects.filter(store=self.store, name='Laptop').count(), 1)
        self.assertEqual(Category.objects.get(store=self.store, name='Gaming').parent.name, 'Laptop')

        # products: SKU2 = Proline code, our own SKU, prices parsed ("18,500.00")
        v = ProductVariant.objects.get(store=self.store, sku2='12511')
        self.assertEqual(v.product.name, 'لابتوب ديل لاتيتيود 5420')
        self.assertEqual((v.cost_price, v.sell_price, v.reorder_level),
                         (Decimal('15000'), Decimal('18500'), Decimal('1')))
        self.assertEqual(v.barcode, '6291041500213')
        self.assertTrue(v.sku and v.sku.endswith('100'))
        self.assertEqual(v.product.category, dell)
        self.assertEqual(v.product.supplier.name, 'شركة النور للتوريدات')
        # a too-long / non-digit code is never cut: imported without SKU2, the tag keeps the code
        for code, name in (('123456789', 'كابل HDMI'), ('AB-77', 'حقيبة لابتوب')):
            ref = ExternalRef.objects.get(store=self.store, system='proline', kind='product', external_key=code)
            pv = ProductVariant.objects.get(pk=ref.object_id)
            self.assertIsNone(pv.sku2)
            self.assertEqual(pv.product.name, name)
        # barcode rules: = code → not stored; already taken → not stored; BarCode used when BarCode1 empty
        self.assertIsNone(ProductVariant.objects.get(store=self.store, sku2='12600').barcode)
        self.assertIsNone(ProductVariant.objects.get(store=self.store, sku2='12607').barcode)
        self.assertEqual(ProductVariant.objects.get(store=self.store, sku2='12601').barcode, 'B-12601')
        # no supplier in Proline → the shop's default; orphan Group2 → falls back to its Group1
        self.assertEqual(ProductVariant.objects.get(store=self.store, sku2='12604').product.supplier.name,
                         'Main supplier')
        self.assertEqual(ProductVariant.objects.get(store=self.store, sku2='12608').product.category.name,
                         'اكسسوارات')

        # suppliers: by type column, by purchases, by item source; party in both → supplier AND customer
        names = set(Supplier.objects.filter(store=self.store).values_list('name', flat=True))
        self.assertEqual(names, {'Main supplier', 'شركة النور للتوريدات', 'مؤسسة الأمل', 'كريم علي', 'مورد بالمصدر'})
        self.assertTrue(all(s.prefix_locked for s in Supplier.objects.filter(store=self.store)))
        self.assertTrue(Customer.objects.filter(store=self.store, name='كريم علي').exists())
        self.assertFalse(Customer.objects.filter(store=self.store, name='مؤسسة الأمل').exists())

        # customers: first phone, credit limit, notes; a taken phone / bad email / bad number never block
        c1 = Customer.objects.get(store=self.store, name='محمد أحمد')
        self.assertEqual((c1.phone_number, c1.credit_limit, c1.notes, c1.city),
                         ('01001234567', Decimal('5000'), 'عميل قديم', 'القاهرة'))
        self.assertIsNone(Customer.objects.get(store=self.store, name='أحمد حسن').phone_number)
        self.assertIsNone(Customer.objects.get(store=self.store, name='منى سعيد').phone_number)
        d = Customer.objects.get(store=self.store, name='Old Debtor')
        self.assertEqual((d.email, d.credit_limit), ('', None))
        self.assertEqual(Customer.objects.get(store=self.store, name='سارة محمود').email, 'sara@example.com')

        # one activity-log entry for the run
        self.assertEqual(ActivityLog.objects.filter(store=self.store, action__startswith='Import from proline').count(), 1)

    def test_second_run_adds_nothing(self):
        self.run_import(commit=True)
        before = snapshot(self.store)
        report = self.run_import(commit=True)
        for k in ('categories', 'suppliers', 'customers', 'products'):
            c = report.counts(k)
            self.assertEqual((c['add'], c['link'], c['update']), (0, 0, 0), (k, report.render(False)))
        self.assertEqual(report.counts('products')['unchanged'], 12)
        self.assertEqual(report.counts('customers')['unchanged'], 7)
        after = snapshot(self.store)
        after.pop('logs'), before.pop('logs')     # the 2nd run logs its own entry
        self.assertEqual(after, before)

    def test_changed_price_in_a_fresher_export_updates_it(self):
        self.run_import(commit=True)
        n = ProductVariant.objects.filter(store=self.store).count()
        folder = self.changed_copy(('tb_Item.csv', ',250,', ',275,'))
        report = self.run_import(commit=True, folder=folder)
        self.assertEqual(report.counts('products')['update'], 1, report.render(False))
        self.assertEqual(report.counts('products')['add'], 0)
        self.assertEqual(ProductVariant.objects.get(store=self.store, sku2='12600').sell_price, Decimal('275'))
        self.assertEqual(ProductVariant.objects.filter(store=self.store).count(), n)

    def test_hand_made_product_with_same_sku2_is_linked_not_duplicated(self):
        self.run_import(commit=True)
        self.assertEqual(ProductVariant.all_objects.filter(store=self.store, sku2='12513').count(), 1)
        v = ProductVariant.objects.get(pk=self.hand_variant.pk)
        ref = ExternalRef.objects.get(store=self.store, kind='product', external_key='12513')
        self.assertEqual((ref.object_id, ref.origin), (v.pk, ExternalRef.Origin.LINKED))
        # prices + reorder level follow Proline; the shop's own name / category / barcode stay
        self.assertEqual((v.cost_price, v.sell_price, v.reorder_level),
                         (Decimal('20000'), Decimal('24000'), Decimal('2')))
        self.assertEqual(v.product.name, 'Lenovo ThinkPad T14 (typed by hand)')
        self.assertEqual(v.product.category.name, 'Laptop')
        self.assertEqual(v.barcode, '12514')
        # 12514 = the hand-made laptop's barcode (a 2nd unit's old code) → linked to the SAME product,
        # its own prices ignored (the SKU2 code owns the prices), no second product
        ref2 = ExternalRef.objects.get(store=self.store, kind='product', external_key='12514')
        self.assertEqual((ref2.object_id, ref2.origin), (v.pk, ExternalRef.Origin.LINKED))
        self.assertEqual(Product.objects.filter(store=self.store, name__startswith='لابتوب لينوفو').count(), 0)
        # the hand-made customer (same phone + same name) is linked, notes kept
        c = Customer.objects.filter(store=self.store, phone_number='01099998888')
        self.assertEqual(c.count(), 1)
        self.assertEqual(c.get().notes, 'typed by hand')

    def test_other_shop_is_never_touched(self):
        before = snapshot(self.other)
        self.run_import(commit=True)
        self.run_import(commit=True)
        self.assertEqual(snapshot(self.other), before)
        self.assertFalse(ExternalRef.all_objects.filter(store=self.other).exists())

    def test_stock_is_never_touched(self):
        adjustments = StockAdjustment.all_objects.count()
        self.run_import(commit=True)
        self.assertEqual(StockAdjustment.all_objects.count(), adjustments)
        self.assertEqual(StockLevel.objects.get(variant=self.hand_variant).quantity, Decimal('2'))
        new = ProductVariant.objects.filter(store=self.store).exclude(pk=self.hand_variant.pk)
        self.assertFalse(StockLevel.objects.filter(variant__in=new).exclude(quantity=0).exists())

    def test_balances_none_writes_no_opening_balance(self):
        self.run_import(commit=True)                         # default = none
        self.assertFalse(Customer.objects.filter(store=self.store).exclude(balance=0).exists())

    def test_balances_opening_writes_customers_only(self):
        report = self.run_import(commit=True, balances='opening')
        bal = dict(Customer.objects.filter(store=self.store).values_list('name', 'balance'))
        self.assertEqual(bal['محمد أحمد'], Decimal('1500'))
        self.assertEqual(bal['Old Debtor'], Decimal('750'))
        self.assertEqual(bal['عميل يدوي'], Decimal('200'))      # linked hand-made customer too
        self.assertEqual(bal['كريم علي'], Decimal('0'))         # customer AND supplier → not split, not set
        self.assertTrue(any('opening' in w for w in report.warnings))


class ReportTests(_Base):
    def test_split_extras_and_dropped_fields_are_reported(self):
        text = self.run_import(commit=False).render(dry_run=True)
        self.assertIn('type column: Type1 (auto', text)
        self.assertIn('→ supplier: used in purchases or as an item source', text)
        self.assertIn('→ customer + supplier: used in both purchases and sales', text)
        self.assertIn('1 codes used in invoices/items match no tb_Cust row (e.g. 999)', text)
        self.assertIn('2 × rows with ANY extra barcode (BarCode2..BarCode9)', text)
        self.assertIn('1 × PriceMini set', text)
        self.assertIn('dropped: NID', text)
        self.assertIn('dropped: GUAName', text)
        self.assertIn('Balance = Debit − Credit on 8 of 8 rows', text)
        self.assertIn('missing columns: web_name', text)
        self.assertIn('extra columns (ignored): FutureColumn', text)
        self.assertIn('bad number in PriceOut1 ("12x")', text)
        self.assertIn('duplicate ItemCode', text)

    def test_forced_codes_override_the_split(self):
        report = self.run_import(commit=False, customer_codes=['12'], type_column='none')
        text = report.render(dry_run=True)
        self.assertIn('forced by --customer-codes', text)
        self.assertIn('type column: off', text)

    def test_commit_refused_while_sku2_is_off(self):
        StoreSettings.objects.filter(store=self.store).update(sku2_state=StoreSettings.Sku2State.OFF)
        before = snapshot(self.store)
        report = self.run_import(commit=True)
        self.assertFalse(report.committed)
        self.assertTrue(any('SKU2 is not ON' in b for b in report.blockers))
        self.assertEqual(snapshot(self.store), before)
        with self.assertRaises(CommandError):
            call_command('import_external', 'proline', '--store', '100', '--dir', FIXTURES, '--commit',
                         stdout=StringIO())


class SearchIndexTests(_Base):
    """A product save pushes to search at once — an import must not leak a dry run into search."""

    def test_dry_run_pushes_nothing_to_search(self):
        with mock.patch('search.indexing.upsert_product') as up, mock.patch('search.indexing.sync_products') as sync:
            with self.captureOnCommitCallbacks(execute=True):
                self.run_import(commit=False)
            up.assert_not_called()
            sync.assert_not_called()
            # the normal per-save push is back after the run
            Product.objects.create(store=self.store, name='after', supplier=self.store.default_supplier)
            self.assertEqual(up.call_count, 1)

    def test_commit_syncs_search_once_committed(self):
        with mock.patch('search.indexing.upsert_product') as up, mock.patch('search.indexing.sync_products') as sync:
            with self.captureOnCommitCallbacks(execute=True):
                self.run_import(commit=True)
            up.assert_not_called()
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(len(sync.call_args[0][0]), 11)    # 10 new + the linked laptop whose prices changed


class HelperTests(TestCase):
    def test_numbers_and_keys(self):
        self.assertEqual(parse_decimal('1,234.50'), Decimal('1234.50'))
        self.assertEqual(parse_decimal('١٢٣'), Decimal('123'))
        self.assertIsNone(parse_decimal(''))
        self.assertIsNone(parse_decimal('NULL'))
        with self.assertRaises(BadNumber):
            parse_decimal('12x', 'PriceOut1')
        self.assertEqual(norm_key(' 12.0 '), '12')
