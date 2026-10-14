"""Code search + barcode rules (2026-10-14):
- the Products list puts an EXACT code (SKU / SKU2 / barcode / pack barcode) first;
- a pack barcode finds its product and the POS is told which pack was scanned;
- a barcode is unique per shop across variants AND packs (server-side, soft-deleted ignored).
"""
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Store, StoreSettings
from inventory.models import Product, ProductUnit, ProductVariant, Supplier
from users.models import User


class _Shop:
    def make_shop(self, code):
        owner = User.objects.create_user(username=f'own{code}', password='pw', role='OWNER')
        store = Store.objects.create(name=f'S{code}', store_code=code, owner=owner)
        owner.store = store
        owner.save()
        settings, _ = StoreSettings.objects.get_or_create(store=store)
        sup = Supplier.objects.create(store=store, name=f'Sup{code}', code_prefix='400', prefix_locked=True)
        api = APIClient()
        api.force_authenticate(owner)
        return store, settings, sup, api

    def product(self, store, sup, name, **variant):
        p = Product.objects.create(store=store, name=name, supplier=sup)
        v = ProductVariant.objects.create(product=p, **variant)
        return p, v

    def pack(self, variant, barcode, name='Pack', factor=10, **kw):
        return ProductUnit.objects.create(variant=variant, name=name, factor=factor,
                                          sell_price=50, barcode=barcode, **kw)


class ProductListExactFirstTests(_Shop, TestCase):
    def setUp(self):
        self.store, self.st, self.sup, self.api = self.make_shop('100')

    def _list(self, q, **extra):
        r = self.api.get('/api/inventory/products/', {'search': q, **extra})
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        rows = data['results'] if isinstance(data, dict) else data
        return [x['name'] for x in rows]

    def test_exact_barcode_beats_a_longer_code_and_a_name_hit(self):
        self.product(self.store, self.sup, 'AAA contains', barcode='9912077')
        self.product(self.store, self.sup, 'AAB 12077 in the name')
        self.product(self.store, self.sup, 'ZZZ exact', barcode='12077')
        self.assertEqual(self._list('12077')[0], 'ZZZ exact')
        # …even when the user sorts the table by name
        self.assertEqual(self._list('12077', ordering='name')[0], 'ZZZ exact')
        self.assertEqual(self._list('12077', ordering='-name')[0], 'ZZZ exact')

    def test_exact_sku_with_dashes_ignored(self):
        self.product(self.store, self.sup, 'AAA longer', sku='9912345667')
        self.product(self.store, self.sup, 'ZZZ dashed', sku='123-456-67')
        self.assertEqual(self._list('12345667', ordering='name')[0], 'ZZZ dashed')

    def test_pack_barcode_finds_its_product(self):
        _, v = self.product(self.store, self.sup, 'Panadol')
        self.pack(v, '6221000000017')
        self.assertEqual(self._list('6221000000017'), ['Panadol'])
        self.assertIn('Panadol', self._list('62210000'))       # contains, too


class PosPackScanTests(_Shop, TestCase):
    def setUp(self):
        self.store, self.st, self.sup, self.api = self.make_shop('100')
        self.p, self.v = self.product(self.store, self.sup, 'Panadol', barcode='111')
        self.unit = self.pack(self.v, '222')

    def _pos(self, q):
        r = self.api.get('/api/inventory/products/autocomplete/', {'q': q, 'pos': '1'})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()['results']

    def test_pack_barcode_names_the_pack(self):
        rows = self._pos('222')
        self.assertEqual(rows[0]['name'], 'Panadol')
        self.assertEqual(rows[0]['scan_unit'], str(self.unit.id))

    def test_piece_barcode_names_no_pack(self):
        rows = self._pos('111')
        self.assertEqual(rows[0]['name'], 'Panadol')
        self.assertIsNone(rows[0]['scan_unit'])

    def test_unsellable_pack_still_finds_the_product_but_names_no_pack(self):
        self.unit.sellable = False
        self.unit.save()
        rows = self._pos('222')
        self.assertEqual(rows[0]['name'], 'Panadol')
        self.assertIsNone(rows[0]['scan_unit'])

    def test_deleted_pack_is_not_found(self):
        self.unit.delete()
        self.assertEqual(self._pos('222'), [])

    def test_shop_without_packs_ignores_pack_barcodes(self):
        self.st.multi_unit_enabled = False
        self.st.save()
        self.assertEqual(self._pos('222'), [])


class BarcodeUniquePerShopTests(_Shop, TestCase):
    def setUp(self):
        self.store, self.st, self.sup, self.api = self.make_shop('100')
        self.p, self.v = self.product(self.store, self.sup, 'Panadol', barcode='111')
        self.unit = self.pack(self.v, '222')

    def _create(self, name, units):
        return self.api.post('/api/inventory/products/',
                             {'name': name, 'supplier': str(self.sup.id), 'selling_units': units},
                             format='json')

    def _unit(self, barcode, name='Box'):
        return {'name': name, 'factor': 10, 'sell_price': 5, 'barcode': barcode}

    def test_new_pack_cannot_reuse_a_variant_or_pack_barcode(self):
        for code in ('111', '222', ' 222 '):
            r = self._create('Other', [self._unit(code)])
            self.assertEqual(r.status_code, 400, code)
            self.assertIn('Panadol', str(r.json()['selling_units']))
            self.assertEqual(r.json()['barcode_code'], [code.strip()])
            self.assertEqual(r.json()['barcode_owner'], ['Panadol'])
        self.assertEqual(self._create('Other', [self._unit('333')]).status_code, 201)

    def test_empty_barcodes_may_repeat(self):
        r = self._create('Other', [self._unit(''), self._unit(None, name='Case')])
        self.assertEqual(r.status_code, 201, r.content)

    def test_two_packs_of_one_product_cannot_share_a_barcode(self):
        r = self._create('Other', [self._unit('333'), self._unit('333', name='Case')])
        self.assertEqual(r.status_code, 400)

    def test_editing_a_product_keeps_its_own_pack_barcode(self):
        r = self.api.patch(f'/api/inventory/products/{self.p.id}/',
                           {'selling_units': [{'id': str(self.unit.id), 'name': 'Pack', 'factor': 10,
                                               'sell_price': 60, 'barcode': '222'}]}, format='json')
        self.assertEqual(r.status_code, 200, r.content)

    def test_soft_deleted_rows_do_not_block(self):
        self.unit.delete()
        self.assertEqual(self._create('B', [self._unit('222')]).status_code, 201)
        self.p.delete()
        self.assertEqual(self._create('C', [self._unit('111')]).status_code, 201)

    def test_another_shop_may_reuse_a_barcode(self):
        _, _, sup2, api2 = self.make_shop('200')
        r = api2.post('/api/inventory/products/',
                      {'name': 'Theirs', 'supplier': str(sup2.id), 'selling_units': [self._unit('222')]},
                      format='json')
        self.assertEqual(r.status_code, 201, r.content)

    def test_variant_endpoint_checks_too(self):
        _, other = self.product(self.store, self.sup, 'Other')
        url = f'/api/inventory/variants/{other.id}/'
        self.assertEqual(self.api.patch(url, {'barcode': '222'}, format='json').status_code, 400)
        self.assertEqual(self.api.patch(url, {'barcode': '111'}, format='json').status_code, 400)
        self.assertEqual(self.api.patch(url, {'barcode': '444'}, format='json').status_code, 200)
        # saving a variant with its own barcode unchanged is fine
        self.assertEqual(self.api.patch(f'/api/inventory/variants/{self.v.id}/', {'barcode': '111'},
                                        format='json').status_code, 200)

    def test_variant_cannot_be_attached_to_another_shops_product(self):
        store2, _, sup2, _ = self.make_shop('200')
        theirs, _ = self.product(store2, sup2, 'Theirs')
        r = self.api.post('/api/inventory/variants/', {'product': str(theirs.id)}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(ProductVariant.objects.filter(product=theirs).count() > 1)
