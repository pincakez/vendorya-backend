"""Import from another system — the Proline adapter, part A (master data), on fake CSVs in Proline's
exact column layout (`fixtures/proline/`, 5–15 rows each, with broken rows on purpose)."""
import csv
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
        # The hand-made laptop in make_shop has its own name, so most tests link it by code on purpose;
        # ReviewFixTests check what happens without the flag.
        options.setdefault('link_by_code', True)
        return run_import(ProlineAdapter, self.store, folder, options, commit=commit)

    def folder_with(self, tables, extra=None):
        """A copy of some fixture tables, plus extra tables written here: {name: [{col: value}, …]}."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for t in tables:
            shutil.copy(os.path.join(FIXTURES, f'{t}.csv'), tmp)
        for name, rows in (extra or {}).items():
            cols = list(rows[0])
            with open(os.path.join(tmp, f'{name}.csv'), 'w', encoding='utf-8-sig', newline='') as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                w.writerows([[r.get(c, '') for c in cols] for r in rows])
        return tmp

    def append_item(self, folder, **cells):
        path = os.path.join(folder, 'tb_Item.csv')
        with open(path, encoding='utf-8-sig', newline='') as fh:
            header = next(csv.reader(fh))
        with open(path, 'a', encoding='utf-8', newline='') as fh:
            csv.writer(fh).writerow([cells.get(c, '') for c in header])

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
        self.assertNotIn('dropped: NID', text)          # s171: national ID + guarantor have a home now
        self.assertNotIn('dropped: GUAName', text)
        self.assertIn('Balance = Debit − Credit on 8 of 8 rows', text)
        self.assertIn('missing columns: web_name', text)
        self.assertIn('extra columns (ignored): FutureColumn', text)
        self.assertIn('bad number in PriceOut1 ("12x")', text)
        self.assertIn('duplicate ItemCode', text)

    def test_extra_customer_fields_land(self):
        self.run_import(commit=True)
        c = Customer.objects.get(store=self.store, name='محمد أحمد')
        self.assertEqual((c.phone_number, c.phone_2, c.national_id), ('01001234567', '0223456789', '29001011234567'))
        self.assertEqual(Customer.objects.get(store=self.store, name='سارة محمود').guarantor_name, 'ضامن')

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


class ReviewFixTests(_Base):
    """surgeon-reviewer: a code on the wrong product, empty prices, the party key column."""
    ALL = ['tb_Group1', 'tb_Group2', 'tb_Cust', 'tb_Item', 'tb_InvoIn', 'tb_InvoInBack', 'tb_InvoOut']

    # 1 ── linking a hand-made product by its code
    def test_code_on_a_differently_named_product_is_not_linked(self):
        report = self.run_import(commit=True, link_by_code=False)
        v = ProductVariant.objects.get(pk=self.hand_variant.pk)
        self.assertEqual(v.sell_price, Decimal('23000'))                     # not touched
        self.assertFalse(ExternalRef.objects.filter(store=self.store, kind='product',
                                                    external_key__in=['12513', '12514']).exists())
        self.assertEqual(ProductVariant.objects.filter(store=self.store, sku2='12513').count(), 1)
        self.assertEqual(report.counts('products')['skip'], 5)
        text = report.render(dry_run=False)
        self.assertIn('not linked: 12513 "لابتوب لينوفو ثينك باد" ≠ "Lenovo ThinkPad T14 (typed by hand)"', text)
        self.assertIn('not linked: 12514', text)

    def test_same_name_links_without_the_flag(self):
        Product.objects.filter(pk=self.hand_variant.product_id).update(name='  لابتوب لينوفو   ثينك باد ')
        report = self.run_import(commit=True, link_by_code=False)
        self.assertEqual(report.counts('products')['link'], 1)               # 12513 (same name, spacing ignored)
        self.assertEqual(ProductVariant.objects.get(pk=self.hand_variant.pk).sell_price, Decimal('24000'))
        self.assertIn('not linked: 12514', report.render(dry_run=False))     # "… - وحدة 2" is another name

    def test_an_imported_products_auto_sku2_is_never_linked_by_code(self):
        StoreSettings.objects.filter(store=self.store).update(sku2_auto_new=True)
        self.run_import(commit=True)
        bag = ProductVariant.objects.get(pk=ExternalRef.objects.get(
            store=self.store, kind='product', external_key='AB-77').object_id)
        self.assertTrue(bag.sku2)                                             # numbered by the shop
        folder = self.folder_with(self.ALL)
        self.append_item(folder, ItemCode=bag.sku2, ItemName='منتج جديد', PriceIn='5', PriceOut1='9', SourceID='5')
        report = self.run_import(commit=True, folder=folder)
        self.assertEqual(report.counts('products')['add'], 1)
        new = ProductVariant.objects.get(pk=ExternalRef.objects.get(
            store=self.store, kind='product', external_key=bag.sku2).object_id)
        self.assertNotEqual(new.pk, bag.pk)
        self.assertNotEqual(new.sku2, bag.sku2)            # the shop may number it afresh, never with the taken code
        bag.refresh_from_db()
        self.assertEqual((bag.product.name, bag.sell_price), ('حقيبة لابتوب', Decimal('350')))
        self.assertIn(f'without SKU2: {bag.sku2} "منتج جديد"', report.render(dry_run=False))

    # 2 ── an empty / zero source price never overwrites a live one; money to 2 decimals
    def test_empty_or_zero_price_keeps_the_price_and_money_is_rounded(self):
        self.run_import(commit=True)
        folder = self.changed_copy(('tb_Item.csv', ',150,250,', ',,0,'))     # 12600: cost empty, sell 0
        with open(os.path.join(folder, 'tb_Item.csv'), encoding='utf-8-sig') as fh:
            data = fh.read().replace(',300,450,', ',300.456,450.555,', 1)     # 12601
        with open(os.path.join(folder, 'tb_Item.csv'), 'w', encoding='utf-8-sig') as fh:
            fh.write(data)
        report = self.run_import(commit=True, folder=folder)
        mouse = ProductVariant.objects.get(store=self.store, sku2='12600')
        self.assertEqual((mouse.cost_price, mouse.sell_price), (Decimal('150'), Decimal('250')))
        charger = ProductVariant.objects.get(store=self.store, sku2='12601')
        self.assertEqual((charger.cost_price, charger.sell_price), (Decimal('300.46'), Decimal('450.56')))
        self.assertEqual(report.counts('products')['update'], 1)
        # the linked hand-made laptop has no MinQty in 12514's row etc. — nothing drifts on a 3rd run
        self.assertEqual(self.run_import(commit=True, folder=folder).counts('products')['update'], 0)

    # 3 ── the customer/supplier key column is fixed after the first import
    def test_party_key_column_is_not_reguessed_by_a_fresher_export(self):
        first = self.run_import(commit=True, folder=self.folder_with(['tb_Group1', 'tb_Group2', 'tb_Cust', 'tb_Item']))
        self.assertTrue(first.committed)
        self.assertTrue(ExternalRef.objects.filter(store=self.store, kind='customer',
                                                   external_key__startswith='ID:').exists())
        # now invoices point at AccountCode-looking codes: a fresh guess would pick AccountCode
        folder = self.folder_with(['tb_Group1', 'tb_Group2', 'tb_Cust', 'tb_Item'], {
            'tb_InvoIn': [{'ComCode': c} for c in ('C2', 'C5', 'C6')],
            'tb_InvoOut': [{'ComCode': c} for c in ('C1', 'C3', 'C6', 'C8', 'C9')],
        })
        n_cust, n_sup = Customer.objects.filter(store=self.store).count(), Supplier.objects.filter(store=self.store).count()
        report = self.run_import(commit=True, folder=folder)
        self.assertEqual((report.counts('customers')['add'], report.counts('suppliers')['add']), (0, 0))
        self.assertEqual(Customer.objects.filter(store=self.store).count(), n_cust)
        self.assertEqual(Supplier.objects.filter(store=self.store).count(), n_sup)
        self.assertIn('tb_Cust key column: ID (kept from the first import', report.render(dry_run=False))


class RealDataFixTests(_Base):
    """s173 — what the real export showed: the price the till charges sits in PriceOut2 for most items, and
    SourceID is empty on every item (the supplier is only on the purchase lines)."""
    MASTER = ['tb_Group1', 'tb_Group2', 'tb_Cust', 'tb_Item', 'tb_InvoIn', 'tb_InvoInBack', 'tb_InvoOut']

    def test_sell_price_column_follows_the_sales_lines(self):
        # 12511: PriceOut1 18,500 · PriceOut2 18000 — the till sold it at 18000 twice (a return line ignored)
        sales = [{'InvoNo': '1', 'ItemCode': '12511', 'Price': '18000', 'Back': ''},
                 {'InvoNo': '2', 'ItemCode': '12511', 'Price': '18000', 'PriceBefore': '18000', 'Back': ''},
                 {'InvoNo': '3', 'ItemCode': '12511', 'Price': '18500', 'Back': 'y'}]
        report = self.run_import(commit=True, folder=self.folder_with(self.MASTER, {'tb_InvoOutData': sales}))
        self.assertTrue(report.committed)
        self.assertIn('sell price column: PriceOut2 (auto', report.render(dry_run=False))
        self.assertEqual(ProductVariant.objects.get(store=self.store, sku2='12511').sell_price, Decimal('18000'))
        # an item with nothing in PriceOut2 takes PriceOut1
        self.assertEqual(ProductVariant.objects.get(store=self.store, sku2='12600').sell_price, Decimal('250'))
        # the owner can force the column
        forced = self.run_import(commit=False, folder=self.folder_with(self.MASTER, {'tb_InvoOutData': sales}),
                                 sell_price='PriceOut1')
        self.assertIn('sell price column: PriceOut1 (--sell-price)', forced.render(dry_run=True))

    def test_no_sales_lines_keeps_priceout1(self):
        report = self.run_import(commit=True)
        self.assertIn('sell price column: PriceOut1 (auto — no sales lines', report.render(dry_run=False))
        self.assertEqual(ProductVariant.objects.get(store=self.store, sku2='12511').sell_price, Decimal('18500'))

    def test_supplier_comes_from_the_latest_purchase_when_sourceid_is_empty(self):
        # 12604 has no SourceID: bought from 6 (old line) then from 5 (newest) → supplier 5 (مؤسسة الأمل)
        lines = [{'InvoNo': '1', 'Date': '2024-01-01', 'ItemCode': '12604', 'ComCode': '6'},
                 {'InvoNo': '2', 'Date': '2025-03-01', 'ItemCode': '12604', 'ComCode': '5'},
                 {'InvoNo': '3', 'Date': '2023-01-01', 'ItemCode': '12604', 'ComCode': '2'}]
        report = self.run_import(commit=True, folder=self.folder_with(self.MASTER, {'tb_InvoInData': lines}))
        self.assertTrue(report.committed)
        screen = ProductVariant.objects.get(store=self.store, sku2='12604').product
        self.assertEqual(screen.supplier.name, 'مؤسسة الأمل')
        self.assertIn('item supplier from latest purchase', report.render(dry_run=False))


class HelperTests(TestCase):
    def test_numbers_and_keys(self):
        self.assertEqual(parse_decimal('1,234.50'), Decimal('1234.50'))
        self.assertEqual(parse_decimal('١٢٣'), Decimal('123'))
        self.assertIsNone(parse_decimal(''))
        self.assertIsNone(parse_decimal('NULL'))
        with self.assertRaises(BadNumber):
            parse_decimal('12x', 'PriceOut1')
        self.assertEqual(norm_key(' 12.0 '), '12')
