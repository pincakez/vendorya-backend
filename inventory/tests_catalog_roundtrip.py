from decimal import Decimal

from django.test import TestCase

from core.models import Address, Branch, Store, StoreSettings
from inventory.import_export import CatalogImporter, export_catalog, parse_csv
from inventory.models import Category, Product, ProductVariant, StockLevel, Supplier
from users.models import User


class _RoundTripBase(TestCase):
    """Two shops (A with 3 products, B empty), same supplier name + branch."""

    def _shop(self, tag, code, prefix):
        owner = User.objects.create_user(username=f'rt_{tag}', password='x')
        store = Store.objects.create(name=f'RT {tag}', store_code=code, owner=owner)
        addr = Address.objects.create(store=store, street_1='1', city='Cairo')
        branch = Branch.objects.create(store=store, name='Main', address=addr)
        Supplier.objects.create(store=store, name='Proline', code_prefix=prefix, prefix_locked=True)
        return store, owner, branch

    def _import(self, store, owner, csv_text):
        headers, rows = parse_csv(csv_text.encode())
        return CatalogImporter(store, owner).commit(headers, rows)

    def _catalog(self, store):
        out = {}
        for v in ProductVariant.all_objects.filter(product__store=store).select_related('product__category'):
            cat = v.product.category
            path = []
            while cat is not None:
                path.insert(0, cat.name)
                cat = cat.parent
            qty = sum((sl.quantity for sl in StockLevel.objects.filter(variant=v)), Decimal('0'))
            out[v.product.name] = (tuple(path), v.cost_price, v.sell_price, qty, v.sku2)
        return out

    def setUp(self):
        self.a, self.a_owner, _ = self._shop('a', '310', '410')
        self.b, self.b_owner, _ = self._shop('b', '320', '420')
        # names that can NOT be rebuilt from BRAND/MODEL: no brand, commas, quotes, Arabic
        res = self._import(self.a, self.a_owner, '\n'.join([
            'M_BRANCH,A_SUPP,P_NAME,M_CAT,S1_CAT,BRAND_DD,Q_QTY,W_PRICE,R_PRICE',
            'Main,Proline,"USB-C Cable, 1m",Accessories,Cables,,5,20,35',
            'Main,Proline,"Mouse ""Pro"" Edition",Accessories,Mice,Logitech,0,150,250',
            'Main,Proline,باراسيتامول 500 مجم,Pharmacy,,,12,10.50,15.75',
        ]))
        self.assertTrue(res['ok'], res)


class CatalogExportImportRoundTripTests(_RoundTripBase):
    """§SKU2 (s165): a catalog exported from one shop imports into another with no errors and the
    names intact. Before, the export had no P_NAME column, so any product without BRAND_DD + MODEL_FT
    came back as "cannot determine a product name" and the whole file was rejected."""

    def test_export_has_p_name(self):
        headers, rows = parse_csv(export_catalog(self.a).encode())
        self.assertIn('P_NAME', headers)
        self.assertCountEqual([r['P_NAME'] for r in rows],
                              ['USB-C Cable, 1m', 'Mouse "Pro" Edition', 'باراسيتامول 500 مجم'])

    def test_exported_file_imports_into_another_shop(self):
        csv_text = export_catalog(self.a)
        headers, rows = parse_csv(csv_text.encode())
        check = CatalogImporter(self.b, self.b_owner).validate(headers, rows)
        self.assertEqual(check['errors'], [])
        res = self._import(self.b, self.b_owner, csv_text)
        self.assertTrue(res['ok'], res)
        self.assertEqual(res['summary']['created'], 3)
        self.assertEqual(self._catalog(self.b), self._catalog(self.a))   # names, categories, prices, stock
        self.assertEqual(Category.objects.filter(store=self.b).count(),
                         Category.objects.filter(store=self.a).count())

    def test_sku2_codes_travel_too(self):
        for store in (self.a, self.b):
            StoreSettings.objects.filter(store=store).update(sku2_state=StoreSettings.Sku2State.ON)
        v = ProductVariant.all_objects.get(product__store=self.a, product__name='USB-C Cable, 1m')
        ProductVariant.all_objects.filter(pk=v.pk).update(sku2='12345')
        self.a.refresh_from_db()
        res = self._import(self.b, self.b_owner, export_catalog(Store.objects.get(pk=self.a.pk)))
        self.assertTrue(res['ok'], res)
        self.assertEqual(ProductVariant.all_objects.get(product__store=self.b,
                                                        product__name='USB-C Cable, 1m').sku2, '12345')

    def test_reimport_into_the_same_shop_is_refused_as_duplicates(self):
        """The importer only CREATES products — a re-import into the source shop is rejected whole
        (all-or-nothing), never half-applied."""
        before = Product.objects.filter(store=self.a).count()
        res = self._import(self.a, self.a_owner, export_catalog(self.a))
        self.assertFalse(res['ok'])
        self.assertTrue(all('already exists' in e for e in res['errors']), res['errors'])
        self.assertEqual(Product.objects.filter(store=self.a).count(), before)


class CatalogRoundTripEdgeTests(_RoundTripBase):
    """s166: the two shapes that still broke the round trip — a product with NO category, and a product
    with several variants (one row per variant, same name → "duplicated in the file")."""

    def _variant_rows(self, store, name):
        return sorted((v.sell_price, v.cost_price) for v in
                      ProductVariant.all_objects.filter(product__store=store, product__name=name))

    def test_product_without_category_round_trips(self):
        loose = Product.objects.create(store=self.a, name='Loose Item', category=None,
                                       supplier=Supplier.objects.get(store=self.a))
        ProductVariant.objects.create(product=loose, cost_price=Decimal('5'), sell_price=Decimal('9'))
        res = self._import(self.b, self.b_owner, export_catalog(self.a))
        self.assertTrue(res['ok'], res)
        self.assertIsNone(Product.objects.get(store=self.b, name='Loose Item').category)

    def test_multi_variant_product_comes_back_as_one_product(self):
        mouse = Product.objects.get(store=self.a, name='Mouse "Pro" Edition')
        ProductVariant.objects.create(product=mouse, cost_price=Decimal('160'), sell_price=Decimal('270'))
        ProductVariant.objects.create(product=mouse, cost_price=Decimal('170'), sell_price=Decimal('290'))
        csv_text = export_catalog(self.a)
        headers, rows = parse_csv(csv_text.encode())
        self.assertIn('P_REF', headers)
        res = self._import(self.b, self.b_owner, csv_text)
        self.assertTrue(res['ok'], res)
        self.assertEqual((res['summary']['products'], res['summary']['variants']), (3, 5))
        self.assertEqual(Product.objects.filter(store=self.b, name='Mouse "Pro" Edition').count(), 1)
        self.assertEqual(self._variant_rows(self.b, 'Mouse "Pro" Edition'),
                         self._variant_rows(self.a, 'Mouse "Pro" Edition'))

    def test_export_without_multi_variants_has_no_p_ref_column(self):
        headers, _ = parse_csv(export_catalog(self.a).encode())
        self.assertNotIn('P_REF', headers)

    def test_p_ref_rows_must_agree_on_name_and_supplier(self):
        res = self._import(self.b, self.b_owner, '\n'.join([
            'A_SUPP,P_NAME,P_REF,M_CAT,W_PRICE,R_PRICE',
            'Proline,Keyboard,7,Accessories,10,20',
            'Proline,Keyboard X,7,Accessories,10,20',
        ]))
        self.assertFalse(res['ok'])
        self.assertTrue(any('P_REF "7"' in e for e in res['errors']), res['errors'])
        self.assertFalse(Product.objects.filter(store=self.b).exists())

    def test_same_name_without_p_ref_is_still_a_duplicate(self):
        res = self._import(self.b, self.b_owner, '\n'.join([
            'A_SUPP,P_NAME,M_CAT,W_PRICE,R_PRICE',
            'Proline,Keyboard,,10,20',
            'Proline,Keyboard,,10,20',
        ]))
        self.assertFalse(res['ok'])
        self.assertTrue(any('duplicated in the file' in e for e in res['errors']), res['errors'])
