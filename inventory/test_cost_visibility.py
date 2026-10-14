"""s167 (Yakot 2026-10-14: "hide cost from cashiers") — no cost figure reaches a role the shop hides
`cost_display` from (CASHIER by default): not on the product list, the product page, the variants API,
expiry batches or a storage location's contents. Managers/owners still get every figure, and a shop that
un-hides cost for cashiers in its field_visibility gets it everywhere again (one switch, not hard-coded)."""
import datetime
from decimal import Decimal as D

from django.conf import settings as dj_settings
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, StoreSettings, Branch, Address
from users.models import User
from inventory.models import (Supplier, Product, ProductVariant, StockLevel, StockBatch,
                              StorageLocation, StorageStock)


class CostHiddenFromCashierTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_cv', password='x', role='OWNER')
        self.store = Store.objects.create(name='CV', store_code='131', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.settings, _ = StoreSettings.objects.get_or_create(store=self.store)
        self.settings.expiry_tracking_enabled = True
        self.settings.save()
        branch = Branch.objects.create(store=self.store, name='Main',
                                       address=Address.objects.create(store=self.store, street_1='1', city='Cairo'))
        self.cashier = User.objects.create_user(username='csh_cv', password='x', role='CASHIER', store=self.store)
        self.manager = User.objects.create_user(username='mgr_cv', password='x', role='MANAGER', store=self.store)
        sup = Supplier.objects.create(store=self.store, name='Dell EG', code_prefix='41', prefix_locked=True,
                                      contact_info='0100')
        self.p = Product.objects.create(store=self.store, name='Latitude 5420', supplier=sup,
                                        base_price=D('9000'), track_expiry=True)
        self.v = ProductVariant.objects.create(product=self.p, cost_price=D('9100'), sell_price=D('12000'))
        StockLevel.objects.create(variant=self.v, branch=branch, quantity=D('3'))
        StockBatch.objects.create(store=self.store, variant=self.v, branch=branch, batch_number='B1',
                                  expiry_date=datetime.date.today() + datetime.timedelta(days=90),
                                  quantity_remaining=D('3'), cost_per_base=D('9100'))
        self.loc = StorageLocation.objects.create(store=self.store, name='Back room')
        StorageStock.objects.create(store=self.store, storage_location=self.loc, variant=self.v,
                                    quantity_remaining=D('1'), cost_at_move=D('9100'))

    def _get(self, user, url):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
        r = c.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.json()

    def _all_payloads(self, user):
        detail = self._get(user, f'/api/inventory/products/{self.p.id}/')
        variants = self._get(user, '/api/inventory/variants/')
        variants = variants.get('results', variants) if isinstance(variants, dict) else variants
        listing = self._get(user, '/api/inventory/products/')
        listing = listing.get('results', listing) if isinstance(listing, dict) else listing
        storage = self._get(user, f'/api/inventory/storage-locations/{self.loc.id}/contents/')
        return detail, variants, listing, storage

    def test_cashier_gets_no_cost_anywhere(self):
        detail, variants, listing, storage = self._all_payloads(self.cashier)
        self.assertNotIn('base_price', detail)
        self.assertNotIn('cost_price', detail['variants'][0])
        self.assertTrue(detail['batches'])
        self.assertNotIn('cost_per_base', detail['batches'][0])
        self.assertNotIn('cost_price', variants[0])
        self.assertNotIn('cost_display', listing[0])
        self.assertNotIn('profit_display', listing[0])
        self.assertNotIn('cost_at_move', storage[0])
        # the money a cashier needs to sell is still there
        self.assertEqual(D(detail['variants'][0]['sell_price']), D('12000'))
        # belt and braces: the cost number appears nowhere in what the cashier received
        self.assertNotIn('9100', str((detail, variants, listing, storage)))
        self.assertNotIn('9000.00', str(detail))

    def test_manager_still_gets_every_cost(self):
        detail, variants, listing, storage = self._all_payloads(self.manager)
        self.assertEqual(D(detail['base_price']), D('9000'))
        self.assertEqual(D(detail['variants'][0]['cost_price']), D('9100'))
        self.assertEqual(D(detail['batches'][0]['cost_per_base']), D('9100'))
        self.assertEqual(D(variants[0]['cost_price']), D('9100'))
        self.assertIn('cost_display', listing[0])
        self.assertEqual(D(storage[0]['cost_at_move']), D('9100'))
        self.assertEqual(detail['supplier_name'], 'Dell EG')

    def test_shop_that_shows_cost_to_cashiers_gets_it_everywhere(self):
        self.settings.field_visibility = {'inventory_products': {'CASHIER': []}}
        self.settings.save()
        detail, variants, listing, storage = self._all_payloads(self.cashier)
        self.assertEqual(D(detail['variants'][0]['cost_price']), D('9100'))
        self.assertEqual(D(variants[0]['cost_price']), D('9100'))
        self.assertIn('cost_display', listing[0])
        self.assertEqual(D(storage[0]['cost_at_move']), D('9100'))

    def test_supplier_follows_the_shops_supplier_column_rule(self):
        detail = self._get(self.cashier, f'/api/inventory/products/{self.p.id}/')
        self.assertEqual(detail['supplier_name'], 'Dell EG')        # not hidden by default
        self.settings.field_visibility = {'inventory_products': {'CASHIER': ['cost_display', 'supplier_name']}}
        self.settings.save()
        detail = self._get(self.cashier, f'/api/inventory/products/{self.p.id}/')
        for f in ('supplier', 'supplier_name', 'supplier_contact'):
            self.assertNotIn(f, detail)
