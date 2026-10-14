import csv
import io
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

    @staticmethod
    def _for_another_shop(csv_text):
        """An export with its SKU column emptied — how a catalog is copied into ANOTHER shop since s167
        (a SKU in the file means "update this shop's product", and shop B has none of shop A's SKUs)."""
        headers, rows = parse_csv(csv_text.encode())
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(headers)
        for r in rows:
            w.writerow(['' if h == 'SKU' else r[h] for h in headers])
        return out.getvalue()

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
        csv_text = self._for_another_shop(export_catalog(self.a))
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
        res = self._import(self.b, self.b_owner, self._for_another_shop(export_catalog(Store.objects.get(pk=self.a.pk))))
        self.assertTrue(res['ok'], res)
        self.assertEqual(ProductVariant.all_objects.get(product__store=self.b,
                                                        product__name='USB-C Cable, 1m').sku2, '12345')

    def test_reimport_into_the_same_shop_changes_nothing(self):
        """s167: the export carries each variant's SKU, so importing it straight back into the source shop
        matches every row to its product — nothing is created and nothing changes."""
        before = Product.objects.filter(store=self.a).count()
        catalog = self._catalog(self.a)
        res = self._import(self.a, self.a_owner, export_catalog(self.a))
        self.assertTrue(res['ok'], res)
        s = res['summary']
        self.assertEqual((s['created'], s['updated'], s['unchanged'], s['stock_ignored']), (0, 0, 3, 0))
        self.assertEqual(Product.objects.filter(store=self.a).count(), before)
        self.assertEqual(self._catalog(self.a), catalog)

    def test_another_shops_export_with_skus_is_refused(self):
        """Shop A's SKUs mean nothing in shop B: every row is "not found in this shop" (with one hint on
        top), shop B gets nothing and shop A is untouched."""
        catalog_a = self._catalog(self.a)
        res = self._import(self.b, self.b_owner, export_catalog(self.a))
        self.assertFalse(res['ok'])
        self.assertIn('another shop', res['errors'][0])
        self.assertEqual(sum('not found in this shop' in e for e in res['errors']), 3)
        self.assertFalse(Product.objects.filter(store=self.b).exists())
        self.assertEqual(self._catalog(self.a), catalog_a)


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
        res = self._import(self.b, self.b_owner, self._for_another_shop(export_catalog(self.a)))
        self.assertTrue(res['ok'], res)
        self.assertIsNone(Product.objects.get(store=self.b, name='Loose Item').category)

    def test_multi_variant_product_comes_back_as_one_product(self):
        mouse = Product.objects.get(store=self.a, name='Mouse "Pro" Edition')
        ProductVariant.objects.create(product=mouse, cost_price=Decimal('160'), sell_price=Decimal('270'))
        ProductVariant.objects.create(product=mouse, cost_price=Decimal('170'), sell_price=Decimal('290'))
        csv_text = self._for_another_shop(export_catalog(self.a))
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


class CatalogReimportUpdateTests(_RoundTripBase):
    """s167 (Yakot 2026-10-14, "prices + name, never stock"): a row whose SKU is one of THIS shop's
    products UPDATES its cost, sell price and name — nothing else. An empty SKU is a new product; a SKU
    the shop doesn't have is an error row (never a silent duplicate)."""

    @staticmethod
    def _edit(csv_text, edits):
        """edits: {sku: {column: new value}} → the same CSV with those cells changed."""
        headers, rows = parse_csv(csv_text.encode())
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(headers)
        for r in rows:
            r.update(edits.get(r['SKU'], {}))
            w.writerow([r[h] for h in headers])
        return out.getvalue()

    def _sku(self, name, store=None):
        return ProductVariant.all_objects.filter(product__store=store or self.a,
                                                 product__name=name).values_list('sku', flat=True).first()

    def test_edit_prices_and_name_then_reimport_updates_in_place(self):
        from unittest import mock
        cable, mouse, para = (self._sku('USB-C Cable, 1m'), self._sku('Mouse "Pro" Edition'),
                              self._sku('باراسيتامول 500 مجم'))
        before_skus = set(ProductVariant.all_objects.filter(product__store=self.a).values_list('sku', flat=True))
        csv_text = self._edit(export_catalog(self.a), {
            cable: {'P_NAME': 'USB-C Cable 1 m (braided)'},
            mouse: {'W_PRICE': '160.00', 'R_PRICE': '275'},
            para:  {'Q_QTY': '99'},                       # stock is never imported for an existing product
        })
        headers, rows = parse_csv(csv_text.encode())

        preview = CatalogImporter(self.a, self.a_owner).validate(headers, rows)
        self.assertTrue(preview['ok'], preview)
        p = preview['summary']
        self.assertEqual((p['products'], p['updated'], p['unchanged'], p['price_changes'], p['name_changes'],
                          p['stock_ignored']), (0, 2, 1, 1, 1, 1))
        self.assertTrue(any('stock adjustment' in w for w in preview['warnings']), preview['warnings'])
        by_sku = {c['sku']: c for c in p['changes']}
        self.assertEqual(by_sku[mouse]['sell'], ['250.00', '275.00'])
        self.assertEqual(by_sku[mouse]['cost'], ['150.00', '160.00'])
        self.assertEqual(by_sku[cable]['new_name'], 'USB-C Cable 1 m (braided)')
        self.assertNotIn('cost', by_sku[cable])

        with mock.patch('search.indexing.sync_products') as sync, self.captureOnCommitCallbacks(execute=True):
            res = self._import(self.a, self.a_owner, csv_text)
        self.assertTrue(res['ok'], res)
        self.assertEqual((res['summary']['created'], res['summary']['updated']), (0, 2))
        # the search index hears about both changed products (the price one fires no signal of its own)
        synced = set(sync.call_args.args[0])
        self.assertEqual(synced, {str(ProductVariant.all_objects.get(sku=s, product__store=self.a).product_id)
                                  for s in (cable, mouse)})

        cat = self._catalog(self.a)
        self.assertEqual(cat['Mouse "Pro" Edition'][1:3], (Decimal('160.00'), Decimal('275.00')))
        self.assertIn('USB-C Cable 1 m (braided)', cat)
        self.assertNotIn('USB-C Cable, 1m', cat)
        self.assertEqual(cat['باراسيتامول 500 مجم'][3], Decimal('12'))     # stock untouched
        self.assertEqual(Product.objects.filter(store=self.a).count(), 3)   # nothing new
        self.assertEqual(set(ProductVariant.all_objects.filter(product__store=self.a)
                             .values_list('sku', flat=True)), before_skus)

    def test_old_file_without_sku_still_creates(self):
        res = self._import(self.a, self.a_owner, '\n'.join([
            'M_BRANCH,A_SUPP,P_NAME,M_CAT,Q_QTY,W_PRICE,R_PRICE',
            'Main,Proline,Keyboard,Accessories,4,100,180',
        ]))
        self.assertTrue(res['ok'], res)
        self.assertEqual((res['summary']['created'], res['summary']['updated'], res['summary']['unchanged']),
                         (1, 0, 0))
        self.assertEqual(self._catalog(self.a)['Keyboard'][1:4], (Decimal('100'), Decimal('180'), Decimal('4')))

    def test_new_rows_and_update_rows_in_one_file(self):
        mouse = self._sku('Mouse "Pro" Edition')
        res = self._import(self.a, self.a_owner, '\n'.join([
            'A_SUPP,P_NAME,M_CAT,W_PRICE,R_PRICE,SKU',
            f'Proline,"Mouse ""Pro"" Edition",Accessories,150,260,{mouse}',
            'Proline,Keyboard,Accessories,100,180,',
        ]))
        self.assertTrue(res['ok'], res)
        self.assertEqual((res['summary']['created'], res['summary']['updated']), (1, 1))
        self.assertEqual(Product.objects.filter(store=self.a).count(), 4)
        self.assertEqual(self._catalog(self.a)['Mouse "Pro" Edition'][2], Decimal('260'))

    def test_unknown_sku_is_an_error_row_and_nothing_is_applied(self):
        mouse = self._sku('Mouse "Pro" Edition')
        catalog = self._catalog(self.a)
        csv_text = self._edit(export_catalog(self.a), {
            mouse: {'R_PRICE': '999'},
            self._sku('USB-C Cable, 1m'): {'SKU': '9999999999'},   # a typo'd SKU
        })
        res = self._import(self.a, self.a_owner, csv_text)
        self.assertFalse(res['ok'])
        self.assertEqual(len(res['errors']), 1, res['errors'])
        self.assertIn('SKU "9999999999" not found in this shop', res['errors'][0])
        self.assertEqual(self._catalog(self.a), catalog)                    # all-or-nothing
        self.assertEqual(Product.objects.filter(store=self.a).count(), 3)

    def test_another_shops_sku_never_touches_that_shop(self):
        res = self._import(self.b, self.b_owner, '\n'.join([
            'A_SUPP,P_NAME,M_CAT,W_PRICE,R_PRICE', 'Proline,B Mouse,Accessories,10,20']))
        self.assertTrue(res['ok'], res)
        b_sku = self._sku('B Mouse', store=self.b)
        a_mouse = self._sku('Mouse "Pro" Edition')
        res = self._import(self.a, self.a_owner, '\n'.join([
            'A_SUPP,P_NAME,M_CAT,W_PRICE,R_PRICE,SKU',
            f'Proline,"Mouse ""Pro"" Edition",Accessories,150,260,{a_mouse}',
            f'Proline,B Mouse,Accessories,10,99,{b_sku}',
        ]))
        self.assertFalse(res['ok'])
        self.assertTrue(any(f'SKU "{b_sku}" not found in this shop' in e for e in res['errors']), res['errors'])
        self.assertEqual(self._catalog(self.b)['B Mouse'][2], Decimal('20'))         # shop B untouched
        self.assertEqual(self._catalog(self.a)['Mouse "Pro" Edition'][2], Decimal('250'))
        self.assertFalse(Product.objects.filter(store=self.a, name='B Mouse').exists())

    def test_multi_variant_product_updates_each_variant_by_its_sku(self):
        mouse = Product.objects.get(store=self.a, name='Mouse "Pro" Edition')
        v1 = mouse.variants.get()
        v2 = ProductVariant.objects.create(product=mouse, cost_price=Decimal('160'), sell_price=Decimal('270'))
        v3 = ProductVariant.objects.create(product=mouse, cost_price=Decimal('170'), sell_price=Decimal('290'))
        new = 'Mouse Pro Edition (2026)'
        csv_text = self._edit(export_catalog(self.a), {
            v1.sku: {'P_NAME': new},
            v2.sku: {'P_NAME': new, 'R_PRICE': '280'},
            v3.sku: {'P_NAME': new},
        })
        self.assertIn('P_REF', parse_csv(csv_text.encode())[0])
        res = self._import(self.a, self.a_owner, csv_text)
        self.assertTrue(res['ok'], res)
        s = res['summary']
        self.assertEqual((s['created'], s['updated'], s['unchanged'], s['price_changes'], s['name_changes']),
                         (0, 3, 2, 1, 1))
        mouse.refresh_from_db()
        self.assertEqual(mouse.name, new)
        self.assertEqual(sorted(v.sell_price for v in ProductVariant.all_objects.filter(product=mouse)),
                         [Decimal('250'), Decimal('280'), Decimal('290')])
        self.assertEqual(Product.objects.filter(store=self.a).count(), 3)

    def test_rows_of_one_product_must_agree_on_the_name(self):
        mouse = Product.objects.get(store=self.a, name='Mouse "Pro" Edition')
        v2 = ProductVariant.objects.create(product=mouse, cost_price=Decimal('160'), sell_price=Decimal('270'))
        csv_text = self._edit(export_catalog(self.a), {v2.sku: {'P_NAME': 'Something else'}})
        res = self._import(self.a, self.a_owner, csv_text)
        self.assertFalse(res['ok'])
        self.assertTrue(any('different P_NAME' in e for e in res['errors']), res['errors'])
        mouse.refresh_from_db()
        self.assertEqual(mouse.name, 'Mouse "Pro" Edition')

    def test_rename_onto_another_products_name_is_refused(self):
        csv_text = self._edit(export_catalog(self.a),
                              {self._sku('USB-C Cable, 1m'): {'P_NAME': 'mouse "pro" edition'}})
        res = self._import(self.a, self.a_owner, csv_text)
        self.assertFalse(res['ok'])
        self.assertTrue(any("can't rename" in e for e in res['errors']), res['errors'])

    def test_commit_endpoint_logs_one_activity_entry_with_the_counts(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from rest_framework.test import APIClient
        from core.models import ActivityLog
        self.a_owner.role, self.a_owner.store = 'OWNER', self.a
        self.a_owner.save()
        mouse = self._sku('Mouse "Pro" Edition')
        csv_text = self._edit(export_catalog(self.a), {mouse: {'R_PRICE': '300'}})
        client = APIClient()
        client.force_authenticate(user=self.a_owner)
        resp = client.post('/api/inventory/catalog/import/commit/',
                           {'file': SimpleUploadedFile('c.csv', csv_text.encode(), content_type='text/csv')},
                           format='multipart')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['summary']['updated'], 1)
        logs = ActivityLog.all_objects.filter(store=self.a)
        self.assertEqual(logs.count(), 1)
        log = logs.get()
        self.assertIn('1 updated', log.action)
        self.assertEqual(log.details['changes'][0]['sell'], ['250.00', '300.00'])
