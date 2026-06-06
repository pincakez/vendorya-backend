from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Store, StoreSettings, Branch, Address
from users.models import User
from inventory.models import (
    Supplier, Product, ProductVariant, StockLevel, StockAdjustment,
    Category, AttributeDefinition, MAX_CATEGORY_DEPTH,
)


class StockAdjustmentNegativeStockTests(TestCase):
    """A manual stock adjustment must obey the store's allow_negative_stock
    policy exactly like a POS sale does — and roll back cleanly when it can't."""

    def setUp(self):
        self.owner = User.objects.create_user(username='owner1', password='x')
        self.store = Store.objects.create(name='S1', store_code='100', owner=self.owner)
        # A post_save signal auto-creates StoreSettings; just grab and configure it.
        self.settings, _ = StoreSettings.objects.get_or_create(store=self.store)
        self.settings.allow_negative_stock = False
        self.settings.save()
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        self.supplier = Supplier.objects.create(
            store=self.store, name='Sup', code_prefix='400', prefix_locked=True)
        product = Product.objects.create(store=self.store, name='Laptop', supplier=self.supplier)
        self.variant = ProductVariant.objects.create(product=product)
        StockLevel.objects.create(variant=self.variant, branch=self.branch, quantity=Decimal('5'))

    def _adjust(self, change):
        return StockAdjustment.objects.create(
            store=self.store, branch=self.branch, variant=self.variant,
            quantity_change=Decimal(change), reason=StockAdjustment.Reason.THEFT,
            adjusted_by=self.owner,
        )

    def _stock(self):
        return StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity

    def test_block_when_negative_not_allowed(self):
        with self.assertRaises(ValidationError):
            self._adjust('-7')
        # Nothing persisted: stock unchanged AND the ledger row rolled back.
        self.assertEqual(self._stock(), Decimal('5'))
        self.assertEqual(StockAdjustment.all_objects.filter(store=self.store).count(), 0)

    def test_allow_when_policy_permits(self):
        self.settings.allow_negative_stock = True
        self.settings.save()
        self._adjust('-7')
        self.assertEqual(self._stock(), Decimal('-2'))

    def test_normal_reduction_within_stock_ok(self):
        self._adjust('-3')
        self.assertEqual(self._stock(), Decimal('2'))

    def test_positive_adjustment_always_ok(self):
        self._adjust('10')
        self.assertEqual(self._stock(), Decimal('15'))


class CategoryTreeTests(TestCase):
    """Category tree is capped at MAX_CATEGORY_DEPTH tiers and rejects cycles."""

    def setUp(self):
        owner = User.objects.create_user(username='catowner', password='x')
        self.store = Store.objects.create(name='S1', store_code='200', owner=owner)

    def _cat(self, name, parent=None):
        return Category.objects.create(store=self.store, name=name, parent=parent)

    def test_depth_cap_blocks_too_deep(self):
        # Build a chain exactly MAX_CATEGORY_DEPTH deep — all should succeed.
        node = None
        chain = []
        for i in range(MAX_CATEGORY_DEPTH):
            node = self._cat(f'tier{i+1}', parent=node)
            chain.append(node)
        # One more tier under the deepest must be rejected.
        with self.assertRaises(ValidationError):
            self._cat('too_deep', parent=chain[-1])

    def test_reparent_pushing_subtree_too_deep_blocked(self):
        a = self._cat('A')                  # tier 1
        b = self._cat('B', parent=a)        # tier 2
        c = self._cat('C', parent=b)        # tier 3  (subtree height of A is 2)
        # Make a tier-2 node, then try to put A (height 2) under it -> 2+1+2=5 > 4.
        x = self._cat('X')                  # tier 1
        y = self._cat('Y', parent=x)        # tier 2
        a.parent = y
        with self.assertRaises(ValidationError):
            a.save()

    def test_cycle_self_parent_blocked(self):
        a = self._cat('A')
        a.parent = a
        with self.assertRaises(ValidationError):
            a.save()

    def test_cycle_descendant_parent_blocked(self):
        a = self._cat('A')
        b = self._cat('B', parent=a)
        a.parent = b           # b is a's child -> would be a cycle
        with self.assertRaises(ValidationError):
            a.save()

    def test_normal_tree_ok(self):
        a = self._cat('A')
        b = self._cat('B', parent=a)
        c = self._cat('C', parent=b)
        self.assertEqual(c.parent_id, b.id)
        self.assertEqual(b.parent_id, a.id)


class CatalogImportTests(TestCase):
    """The CSV importer: strict validation + transactional create."""

    def setUp(self):
        self.owner = User.objects.create_user(username='impowner', password='x')
        self.store = Store.objects.create(name='S1', store_code='300', owner=self.owner)
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        Supplier.objects.create(store=self.store, name='Yakot', code_prefix='400', prefix_locked=True)

    def _imp(self):
        from inventory.import_export import CatalogImporter
        return CatalogImporter(self.store, self.owner)

    HEADER = ('M_BRANCH,A_SUPP,M_CAT,S1_CAT,BRAND_DD,MODEL_FT,RAM_DD,Q_QTY,W_PRICE,R_PRICE')

    def _rows(self, *lines):
        from inventory.import_export import parse_csv
        return parse_csv(('\n'.join((self.HEADER,) + lines)).encode())

    def test_good_file_imports(self):
        headers, rows = self._rows(
            'Main,Yakot,Laptop,Business,DELL,Latitude 5480,16 GB,1,"13,600.00","13,700.00"',
            'Main,Yakot,Laptop,Workstation,HP,ZBook G6,32 GB,2,"26,600.00","26,700.00"',
        )
        res = self._imp().commit(headers, rows)
        self.assertTrue(res['ok'], res)
        self.assertEqual(res['summary']['created'], 2)
        self.assertEqual(Product.objects.filter(store=self.store).count(), 2)
        self.assertEqual(ProductVariant.all_objects.filter(product__store=self.store).count(), 2)
        # category tree built: Laptop > Business / Workstation
        self.assertEqual(Category.objects.filter(store=self.store, parent=None).count(), 1)
        self.assertEqual(Category.objects.filter(store=self.store).exclude(parent=None).count(), 2)
        # attributes created (BRAND dropdown collected options)
        brand = AttributeDefinition.objects.get(store=self.store, key='brand')
        self.assertEqual(brand.input_type, AttributeDefinition.InputType.SELECT)
        self.assertCountEqual(brand.options, ['DELL', 'HP'])
        # stock posted via adjustment
        p = Product.objects.get(store=self.store, name='DELL Latitude 5480')
        v = p.variants.first()
        self.assertEqual(StockLevel.objects.get(variant=v, branch=self.branch).quantity, Decimal('1'))
        self.assertTrue(v.sku.startswith('300400'))   # store 300 + supplier 400

    def test_unknown_supplier_rejected(self):
        headers, rows = self._rows(
            'Main,Ghost,Laptop,Business,DELL,X,16 GB,1,100,200')
        res = self._imp().validate(headers, rows)
        self.assertFalse(res['ok'])
        self.assertTrue(any('does not exist' in e for e in res['errors']))

    def test_duplicate_same_supplier_rejected(self):
        headers, rows = self._rows(
            'Main,Yakot,Laptop,Business,DELL,Latitude 5480,16 GB,1,100,200',
            'Main,Yakot,Laptop,Business,DELL,Latitude 5480,8 GB,1,100,200')
        res = self._imp().validate(headers, rows)
        self.assertFalse(res['ok'])
        self.assertTrue(any('duplicated' in e for e in res['errors']))

    def test_depth_over_4_rejected(self):
        from inventory.import_export import parse_csv
        bad = 'M_BRANCH,A_SUPP,M_CAT,S1_CAT,S2_CAT,S3_CAT,S4_CAT,W_PRICE,R_PRICE'
        headers, rows = parse_csv((bad + '\nMain,Yakot,a,b,c,d,e,1,2').encode())
        res = self._imp().validate(headers, rows)
        self.assertFalse(res['ok'])
        self.assertTrue(any('S4_CAT' in e for e in res['errors']))

    def test_unknown_column_rejected(self):
        from inventory.import_export import parse_csv
        bad = 'A_SUPP,M_CAT,WHATEVER,W_PRICE,R_PRICE'
        headers, rows = parse_csv((bad + '\nYakot,Laptop,x,1,2').encode())
        res = self._imp().validate(headers, rows)
        self.assertFalse(res['ok'])
        self.assertTrue(any('Unknown column' in e for e in res['errors']))

    def test_negative_margin_is_warning_not_error(self):
        headers, rows = self._rows(
            'Main,Yakot,Laptop,Business,DELL,Latitude 5480,16 GB,1,"13,700.00","13,600.00"')
        res = self._imp().validate(headers, rows)
        self.assertTrue(res['ok'], res)
        self.assertTrue(any('negative margin' in w for w in res['warnings']))

    def test_export_roundtrips(self):
        from inventory.import_export import export_catalog, parse_csv
        headers, rows = self._rows(
            'Main,Yakot,Laptop,Business,DELL,Latitude 5480,16 GB,3,"13,600.00","13,700.00"')
        self._imp().commit(headers, rows)
        csv_text = export_catalog(self.store)
        self.assertIn('SKU', csv_text)
        self.assertIn('DELL', csv_text)
        h2, r2 = parse_csv(csv_text.encode())
        self.assertEqual(len(r2), 1)
        self.assertIn('BRAND_DD', h2)


class CategoryApiTests(TestCase):
    """The depth guard must surface as a clean 400 over the API, not a 500."""

    def setUp(self):
        from rest_framework.test import APIClient
        self.owner = User.objects.create_user(username='apiowner', password='x')
        self.store = Store.objects.create(name='S1', store_code='201', owner=self.owner)
        self.owner.store = self.store
        self.owner.role = User.Role.OWNER
        self.owner.save()
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def _post(self, name, parent=None):
        return self.client.post('/api/inventory/categories/',
                                {'name': name, 'parent': parent}, format='json')

    def test_too_deep_returns_400(self):
        ids = []
        parent = None
        for i in range(MAX_CATEGORY_DEPTH):
            r = self._post(f't{i}', parent)
            self.assertEqual(r.status_code, 201, r.content)
            parent = r.json()['id']
            ids.append(parent)
        # 5th tier -> blocked with a 400, not a server error.
        r = self._post('too_deep', parent)
        self.assertEqual(r.status_code, 400, r.content)

    def test_delete_with_children_blocked(self):
        a = self._post('A').json()['id']
        self._post('B', a)
        r = self.client.delete(f'/api/inventory/categories/{a}/')
        self.assertEqual(r.status_code, 400, r.content)

    def test_contents_counts(self):
        a = self._post('A').json()['id']
        b = self._post('B', a).json()['id']
        Product.objects.create(store=self.store, name='P1', category_id=b)
        d = self.client.get(f'/api/inventory/categories/{a}/contents/').json()
        self.assertEqual(d['subcategory_count'], 1)
        self.assertEqual(d['product_count'], 1)
        self.assertIsNone(d['parent'])

    def test_move_up_lifts_products_and_subcats(self):
        a = self._post('A').json()['id']
        b = self._post('B', a).json()['id']
        c = self._post('C', b).json()['id']             # A > B > C
        prod = Product.objects.create(store=self.store, name='P1', category_id=b)
        r = self.client.post(f'/api/inventory/categories/{b}/resolve-delete/',
                             {'mode': 'move'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        prod.refresh_from_db()
        self.assertEqual(str(prod.category_id), a)       # product lifted to A
        self.assertEqual(str(Category.objects.get(id=c).parent_id), a)  # C lifted to A
        self.assertFalse(Category.objects.filter(id=b).exists())        # B soft-deleted

    def test_purge_requires_reason(self):
        a = self._post('A').json()['id']
        Product.objects.create(store=self.store, name='P1', category_id=a)
        r = self.client.post(f'/api/inventory/categories/{a}/resolve-delete/',
                             {'mode': 'purge'}, format='json')
        self.assertEqual(r.status_code, 400, r.content)

    def test_purge_soft_deletes_branch_and_products(self):
        a = self._post('A').json()['id']
        b = self._post('B', a).json()['id']
        prod = Product.objects.create(store=self.store, name='P1', category_id=b)
        r = self.client.post(f'/api/inventory/categories/{a}/resolve-delete/',
                             {'mode': 'purge', 'reason': 'DISCONTINUED', 'note': 'cleanup'},
                             format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(Category.objects.filter(id__in=[a, b]).exists())
        self.assertFalse(Product.objects.filter(id=prod.id).exists())   # soft-deleted
        archived = Product.all_objects.get(id=prod.id)
        self.assertTrue(archived.is_deleted)
        self.assertEqual(archived.delete_reason, 'DISCONTINUED')
        self.assertEqual(archived.deleted_by_id, self.owner.id)
