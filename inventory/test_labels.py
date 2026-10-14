"""Price stickers from the product list (s167, PROLINE-PARITY Upgrade 14):
POST /api/inventory/products/label-data/ {ids} → the same item shape as a purchase's label-data."""
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Address, Branch, Store, StoreSettings
from inventory.models import (AttributeDefinition, Product, ProductAttribute, ProductVariant,
                              StockLevel, Supplier)
from users.models import User

URL = '/api/inventory/products/label-data/'


class ProductLabelDataTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='lbl_own', password='x', role='OWNER')
        self.store = Store.objects.create(name='Label Shop', store_code='310', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.cashier = User.objects.create_user(username='lbl_csh', password='x', role='CASHIER', store=self.store)
        self.settings, _ = StoreSettings.objects.get_or_create(store=self.store)
        self.sup = Supplier.objects.create(store=self.store, name='Sup', code_prefix='400', prefix_locked=True)
        self.branch = Branch.objects.create(store=self.store, name='Main',
                                            address=Address.objects.create(store=self.store, street_1='1', city='Cairo'))
        self.laptop = Product.objects.create(store=self.store, name='Laptop', supplier=self.sup)
        self.lv = ProductVariant.objects.create(product=self.laptop, sku='111', sku2='12077', barcode='',
                                                sell_price=Decimal('25000'))
        StockLevel.objects.create(variant=self.lv, branch=self.branch, quantity=Decimal('3'))

    def _post(self, user, ids):
        c = APIClient()
        c.force_authenticate(user)
        return c.post(URL, {'ids': ids}, format='json')

    def test_one_item_per_product_with_stock_labels(self):
        r = self._post(self.owner, [str(self.laptop.id)])
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertEqual(data['store_name'], 'Label Shop')
        [item] = data['items']
        self.assertEqual(item['product_name'], 'Laptop')
        self.assertEqual(item['sku'], '111')
        self.assertEqual(item['barcode'], '111')        # no barcode → the SKU is scanned
        self.assertEqual(item['quantity'], 1)
        self.assertEqual(item['stock_labels'], 3)
        self.assertEqual(item['sku2'], '')              # SKU2 off → not on the sticker

    def test_sku2_only_when_on_and_ticked_for_stickers(self):
        self.settings.sku2_state = StoreSettings.Sku2State.ON
        self.settings.sku2_print_label = True
        self.settings.save()
        [item] = self._post(self.owner, [str(self.laptop.id)]).json()['items']
        self.assertEqual(item['sku2'], '12077')

    def test_multi_variant_one_item_each_named_by_attributes(self):
        shirt = Product.objects.create(store=self.store, name='Shirt', supplier=self.sup)
        color = AttributeDefinition.objects.create(store=self.store, name='Color', key='color')
        for c in ('Red', 'Blue'):
            v = ProductVariant.objects.create(product=shirt, sku=f'S-{c}', sell_price=Decimal('100'))
            ProductAttribute.objects.create(variant=v, definition=color, value=c)
        items = self._post(self.owner, [str(shirt.id)]).json()['items']
        self.assertEqual(sorted(i['product_name'] for i in items), ['Shirt · Blue', 'Shirt · Red'])
        self.assertTrue(all(i['stock_labels'] == 1 for i in items))   # no stock → still 1

    def test_weight_product_gets_one_label(self):
        cheese = Product.objects.create(store=self.store, name='Cheese', supplier=self.sup,
                                        selling_mode=Product.SellingMode.WEIGHT)
        v = ProductVariant.objects.create(product=cheese, sku='222', sell_price=Decimal('300'))
        StockLevel.objects.create(variant=v, branch=self.branch, quantity=Decimal('12.5'))
        [item] = self._post(self.owner, [str(cheese.id)]).json()['items']
        self.assertTrue(item['by_weight'])
        self.assertEqual(item['stock_labels'], 1)

    def test_cashier_may_print(self):
        self.assertEqual(self._post(self.cashier, [str(self.laptop.id)]).status_code, 200)

    def test_other_shop_products_are_invisible(self):
        other_owner = User.objects.create_user(username='lbl_o2', password='x', role='OWNER')
        other = Store.objects.create(name='Other', store_code='311', owner=other_owner)
        other_owner.store = other
        other_owner.save(update_fields=['store'])
        self.assertEqual(self._post(other_owner, [str(self.laptop.id)]).json()['items'], [])

    def test_bad_and_too_many_ids(self):
        self.assertEqual(self._post(self.owner, []).status_code, 400)
        too_many = [str(self.laptop.id)] * (2001)
        self.assertEqual(self._post(self.owner, too_many).status_code, 400)
