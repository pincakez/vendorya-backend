"""SKU setup + SKU2 + exact-match code search (s159, Yakot 2026-10-07 — TODO §SKU-SETUP, §SKU2, §PROLINE-WALLS)."""
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Store, StoreSettings
from inventory.models import Product, ProductVariant, Supplier
from inventory.serializers import SupplierSerializer
from inventory.sku import next_free_supplier_code, supplier_code_width
from users.models import User


class _Shop:
    def make_shop(self, code, name=None):
        owner = User.objects.create_user(username=f'own{code}', password='pw', role='OWNER')
        store = Store.objects.create(name=name or f'S{code}', store_code=code, owner=owner)
        owner.store = store
        owner.save()
        settings, _ = StoreSettings.objects.get_or_create(store=store)
        return store, settings, owner

    def supplier(self, store, code):
        return Supplier.objects.create(store=store, name=f'Sup{code}', code_prefix=code, prefix_locked=True)

    def new_variant(self, store, supplier, **kw):
        p = Product.objects.create(store=store, name='Item', supplier=supplier)
        return ProductVariant.objects.create(product=p, **kw)


class SkuMakerTests(_Shop, TestCase):
    def setUp(self):
        self.store, self.st, self.owner = self.make_shop('100')
        self.sup = self.supplier(self.store, '400')

    def test_defaults_make_the_old_ten_digit_sku(self):
        v = self.new_variant(self.store, self.sup)
        self.assertEqual(v.sku, '0001400100')
        self.assertEqual((v.sku_number, v.sku_prefix, v.store_id), (1, '400', self.store.id))
        self.assertEqual(self.new_variant(self.store, self.sup).sku, '0002400100')

    def test_counts_on_from_a_backfilled_old_sku(self):
        self.new_variant(self.store, self.sup, sku='0041400100', sku_number=41, sku_prefix='400')
        self.assertEqual(self.new_variant(self.store, self.sup).sku, '0042400100')

    def test_product_part_grows_a_digit_when_it_runs_out(self):
        self.st.sku_product_digits = 3
        self.st.save()
        self.new_variant(self.store, self.sup, sku='999400100', sku_number=999, sku_prefix='400')
        v = self.new_variant(self.store, self.sup)
        self.assertEqual(v.sku, '1000400100')
        self.assertEqual(v.sku_number, 1000)

    def test_dashes(self):
        self.st.sku_dashes = True
        self.st.sku_shop_code = '10'
        self.st.save()
        self.assertEqual(self.new_variant(self.store, self.sup).sku, '0001-400-10')

    def test_a_clash_between_two_splits_is_skipped(self):
        # 123 + 456 + 67 and 1234 + 56 + 67 both read 12345667 → the second takes the next free number.
        self.st.sku_shop_code = '67'
        self.st.save()
        sup_a, sup_b = self.supplier(self.store, '456'), self.supplier(self.store, '56')
        self.new_variant(self.store, sup_a, sku='12345667', sku_number=123, sku_prefix='456')
        self.new_variant(self.store, sup_b, sku='12335667', sku_number=1233, sku_prefix='56')
        v = self.new_variant(self.store, sup_b)
        self.assertEqual((v.sku, v.sku_number), ('12355667', 1235))

    def test_two_shops_may_share_a_sku(self):
        other, other_st, _ = self.make_shop('200')
        for st in (self.st, other_st):
            st.sku_shop_code = '10'
            st.save()
        a = self.new_variant(self.store, self.sup)
        b = self.new_variant(other, self.supplier(other, '400'))
        self.assertEqual(a.sku, b.sku)

    def test_random_mode_stays_unique_and_in_width(self):
        self.st.product_numbering_mode = 'RANDOM'
        self.st.sku_product_digits = 3
        self.st.save()
        skus = {self.new_variant(self.store, self.sup).sku for _ in range(20)}
        self.assertEqual(len(skus), 20)
        self.assertTrue(all(len(s) == 9 for s in skus))


class SupplierCodeTests(_Shop, TestCase):
    def setUp(self):
        self.store, self.st, self.owner = self.make_shop('100')

    def _request(self):
        return type('R', (), {'user': self.owner})()

    def test_two_digit_codes_then_grow_to_three(self):
        self.st.sku_supplier_digits = 2
        self.st.save()
        self.assertEqual(supplier_code_width(self.store), 2)
        self.assertEqual(next_free_supplier_code(self.store), '10')
        Supplier.objects.bulk_create([Supplier(store=self.store, name=str(n), code_prefix=str(n), prefix_locked=True)
                                      for n in range(10, 100)])
        self.assertEqual(supplier_code_width(self.store), 3)
        self.assertEqual(next_free_supplier_code(self.store), '100')

    def test_new_code_must_have_the_current_width(self):
        self.st.sku_supplier_digits = 2
        self.st.save()
        bad = SupplierSerializer(data={'name': 'X', 'code_prefix': '123'}, context={'request': self._request()})
        self.assertFalse(bad.is_valid())
        good = SupplierSerializer(data={'name': 'X', 'code_prefix': '12'}, context={'request': self._request()})
        self.assertTrue(good.is_valid(), good.errors)


class CodeSearchTests(_Shop, TestCase):
    def setUp(self):
        self.store, self.st, self.owner = self.make_shop('100')
        self.sup = self.supplier(self.store, '400')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def _pos(self, q):
        r = self.client.get('/api/inventory/products/autocomplete/', {'q': q, 'pos': '1'})
        self.assertEqual(r.status_code, 200, r.content)
        return [x['name'] for x in r.json()['results']]

    def _named(self, name, **variant):
        p = Product.objects.create(store=self.store, name=name, supplier=self.sup)
        ProductVariant.objects.create(product=p, **variant)
        return p

    def test_an_exact_code_beats_one_that_only_contains_it(self):
        self._named('Longer', barcode='9912077')
        self._named('Exact', barcode='12077')
        self.assertEqual(self._pos('12077')[0], 'Exact')

    def test_dashes_are_ignored_when_typed(self):
        self._named('Dashed', sku='123-456-67')
        self.assertIn('Dashed', self._pos('12345667'))
        r = self.client.get('/api/inventory/products/', {'search': '12345667'})
        self.assertIn('Dashed', [x['name'] for x in r.json()])


class Sku2Tests(_Shop, TestCase):
    def setUp(self):
        self.store, self.st, self.owner = self.make_shop('100')
        self.sup = self.supplier(self.store, '400')
        self.sudo = User.objects.create_user(username='sudo', password='sudopw', is_superadmin=True)
        self.admin = APIClient()
        self.admin.force_authenticate(self.sudo)
        self.shop = APIClient()
        self.shop.force_authenticate(self.owner)
        self.url = f'/api/admin/stores/{self.store.id}/sku/'

    def _turn_on(self, **extra):
        r = self.admin.patch(self.url, {'sku2_state': 'ON', **extra}, format='json')
        self.assertEqual(r.status_code, 200, r.content)

    def _create(self, name, **extra):
        return self.shop.post('/api/inventory/products/', {'name': name, 'supplier': str(self.sup.id), **extra},
                              format='json')

    def test_off_by_default_and_refused_on_a_product(self):
        self.assertEqual(self.shop.get('/api/core/settings/').json()['sku2_state'], 'OFF')
        self.assertEqual(self._create('A', sku2='12077').status_code, 400)

    def test_shop_cannot_switch_it_on_itself(self):
        self.shop.patch('/api/core/settings/', {'sku2_state': 'ON'}, format='json')
        self.st.refresh_from_db()
        self.assertEqual(self.st.sku2_state, 'OFF')

    def test_manual_then_sequence_continues_after_the_highest(self):
        self._turn_on(sku2_auto_new=True)
        self.assertEqual(self._create('Old', sku2='12077').status_code, 201)
        self._create('New')
        new = ProductVariant.objects.get(product__name='New', product__source='STORE')
        self.assertEqual(new.sku2, '12078')
        self.assertEqual(self._create('Dup', sku2='12077').status_code, 400)

    def test_found_by_scan_only_while_on(self):
        self._turn_on()
        self._create('Laptop', sku2='12015')
        r = self.shop.get('/api/inventory/products/autocomplete/', {'q': '12015', 'pos': '1'})
        self.assertEqual(r.json()['results'][0]['name'], 'Laptop')
        self.assertEqual(self.admin.post(f'/api/admin/stores/{self.store.id}/sku2/disable/',
                                         {'password': 'sudopw'}, format='json').status_code, 200)
        self.assertEqual(ProductVariant.objects.get(product__name='Laptop', product__source='STORE').sku2, '12015')   # kept as history
        r = self.shop.get('/api/inventory/products/autocomplete/', {'q': '12015', 'pos': '1'})
        self.assertNotIn('Laptop', [x['name'] for x in r.json()['results']])

    def test_disable_needs_the_password_and_is_final(self):
        self._turn_on()
        bad = self.admin.post(f'/api/admin/stores/{self.store.id}/sku2/disable/', {'password': 'nope'}, format='json')
        self.assertEqual(bad.status_code, 403)
        self._create('Laptop', sku2='12015')
        ok = self.admin.post(f'/api/admin/stores/{self.store.id}/sku2/disable/',
                             {'password': 'sudopw', 'delete_history': True}, format='json')
        self.assertEqual(ok.status_code, 200)
        self.assertIsNone(ProductVariant.objects.get(product__name='Laptop', product__source='STORE').sku2)
        again = self.admin.patch(self.url, {'sku2_state': 'ON'}, format='json')
        self.assertEqual(again.status_code, 400)

    def test_on_cannot_go_back_to_off_quietly(self):
        self._turn_on()
        self.assertEqual(self.admin.patch(self.url, {'sku2_state': 'OFF'}, format='json').status_code, 400)

    def test_setup_change_after_skus_needs_the_password(self):
        self.assertEqual(self.admin.patch(self.url, {'sku_dashes': True}, format='json').status_code, 200)
        self._create('First')
        r = self.admin.patch(self.url, {'sku_product_digits': 5}, format='json')
        self.assertEqual(r.status_code, 403)
        r = self.admin.patch(self.url, {'sku_product_digits': 5, 'password': 'sudopw'}, format='json')
        self.assertEqual(r.status_code, 200)

    def test_shop_cannot_reach_the_sudo_page(self):
        self.assertEqual(self.shop.get(self.url).status_code, 403)


class Sku2CsvTests(_Shop, TestCase):
    def setUp(self):
        from core.models import Address, Branch
        self.store, self.st, self.owner = self.make_shop('100')
        self.sup = self.supplier(self.store, '400')
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        Branch.objects.create(store=self.store, name='Main', address=addr)

    def _import(self, csv_text):
        from inventory.import_export import CatalogImporter, parse_csv
        headers, rows = parse_csv(csv_text.encode())
        return CatalogImporter(self.store, self.owner).commit(headers, rows)

    def test_sku2_column_needs_sku2_on(self):
        r = self._import('A_SUPP,M_CAT,P_NAME,W_PRICE,R_PRICE,SKU2\nSup400,Laptops,ZBook,10,20,12015\n')
        self.assertFalse(r['ok'])

    def test_sku2_column_imports_and_exports(self):
        from inventory.import_export import export_catalog
        self.st.sku2_state = 'ON'
        self.st.save()
        r = self._import('A_SUPP,M_CAT,P_NAME,W_PRICE,R_PRICE,SKU2\nSup400,Laptops,ZBook,10,20,12015\n')
        self.assertTrue(r['ok'], r)
        self.assertEqual(ProductVariant.objects.get(product__name='ZBook', product__source='STORE').sku2, '12015')
        self.store.refresh_from_db()
        out = export_catalog(Store.objects.get(pk=self.store.pk))
        self.assertIn('SKU2', out.splitlines()[0])
        self.assertIn('12015', out)
        # the exported file's SKU column is no longer refused as "unknown", and its SKU2 is checked
        again = self._import(out)
        self.assertFalse(any('Unknown column' in e for e in again['errors']), again)
        self.assertTrue(any('already belongs' in e for e in again['errors']), again)
