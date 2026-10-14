"""§WT — default weight DISPLAY unit (KG ↔ g).

Storage never changes: a weight product's base unit is the kilogram, kept to the
gram. The store setting only says how weights are shown/typed; the receipt
payload flags weight lines so the print view can say "250 g" or "0.25 kg".
"""
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, StoreSettings, Branch, Address
from finance.models import SalesInvoice, SalesInvoiceItem
from inventory.models import Supplier, Product, ProductVariant
from users.models import User, Customer

SETTINGS_URL = '/api/core/settings/'


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


def _shop(tag, code):
    owner = User.objects.create_user(username=f'own_{tag}', password='x', role='OWNER')
    store = Store.objects.create(name=f'W{tag}', store_code=code, owner=owner)
    owner.store = store
    owner.save(update_fields=['store'])
    StoreSettings.objects.get_or_create(store=store)
    return owner, store


class DefaultWeightUnitSettingTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner_a, self.store_a = _shop('wa', '201')
        self.owner_b, self.store_b = _shop('wb', '202')

    def test_default_is_kg(self):
        r = _client(self.owner_a).get(SETTINGS_URL)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['default_weight_unit'], 'KG')

    def test_owner_can_switch_to_grams_and_back(self):
        c = _client(self.owner_a)
        r = c.patch(SETTINGS_URL, {'default_weight_unit': 'G'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['default_weight_unit'], 'G')
        self.assertEqual(StoreSettings.objects.get(store=self.store_a).default_weight_unit, 'G')
        r = c.patch(SETTINGS_URL, {'default_weight_unit': 'KG'}, format='json')
        self.assertEqual(r.data['default_weight_unit'], 'KG')

    def test_bad_unit_refused(self):
        for bad in ('MG', 'lb', '', 'g '):
            r = _client(self.owner_a).patch(SETTINGS_URL, {'default_weight_unit': bad}, format='json')
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(StoreSettings.objects.get(store=self.store_a).default_weight_unit, 'KG')

    def test_store_isolated(self):
        _client(self.owner_a).patch(SETTINGS_URL, {'default_weight_unit': 'G'}, format='json')
        self.assertEqual(_client(self.owner_b).get(SETTINGS_URL).data['default_weight_unit'], 'KG')
        self.assertEqual(StoreSettings.objects.get(store=self.store_b).default_weight_unit, 'KG')

    def test_cashier_reads_it_but_cannot_change_it(self):
        cashier = User.objects.create_user(username='csh_w', password='x', role='CASHIER', store=self.store_a)
        c = _client(cashier)
        self.assertEqual(c.get(SETTINGS_URL).data['default_weight_unit'], 'KG')
        self.assertEqual(c.patch(SETTINGS_URL, {'default_weight_unit': 'G'}, format='json').status_code, 403)


class WeightReceiptPayloadTests(TestCase):
    """print-data flags weight lines + carries the store's weight unit; quantity stays kg."""

    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner, self.store = _shop('wr', '203')
        self.settings = StoreSettings.objects.get(store=self.store)
        self.settings.weight_selling_enabled = True
        self.settings.save()
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        sup = Supplier.objects.create(store=self.store, name='Sup', code_prefix='402', prefix_locked=True)
        cheese = Product.objects.create(store=self.store, name='Cheese', supplier=sup, selling_mode='WEIGHT')
        mouse = Product.objects.create(store=self.store, name='Mouse', supplier=sup)
        v_cheese = ProductVariant.objects.create(product=cheese, sell_price=Decimal('200'))
        v_mouse = ProductVariant.objects.create(product=mouse, sell_price=Decimal('100'))
        customer = Customer.objects.create(store=self.store, name='Buyer', phone_number='0101')
        self.inv = SalesInvoice.objects.create(store=self.store, branch=branch, customer=customer,
                                               status=SalesInvoice.Status.DRAFT, date=timezone.now())
        SalesInvoiceItem.objects.create(invoice=self.inv, variant=v_cheese, quantity=Decimal('0.250'),
                                        unit_price=Decimal('200'))
        SalesInvoiceItem.objects.create(invoice=self.inv, variant=v_mouse, quantity=Decimal('2'),
                                        unit_price=Decimal('100'))
        self.url = f'/api/finance/invoices/{self.inv.id}/print-data/'

    def _items(self, data):
        return {it['name']: it for it in data['items']}

    def test_weight_line_flagged_and_unit_sent(self):
        self.settings.default_weight_unit = 'G'
        self.settings.save()
        r = _client(self.owner).get(self.url)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['weight_unit'], 'G')
        items = self._items(r.data)
        self.assertTrue(items['Cheese']['is_weight'])
        self.assertEqual(Decimal(items['Cheese']['quantity']), Decimal('0.250'))  # stored kg, untouched
        self.assertFalse(items['Mouse']['is_weight'])

    def test_default_kg(self):
        r = _client(self.owner).get(self.url)
        self.assertEqual(r.data['weight_unit'], 'KG')

    def test_master_switch_off_means_no_weight_lines(self):
        self.settings.weight_selling_enabled = False
        self.settings.save()
        r = _client(self.owner).get(self.url)
        self.assertFalse(any(it['is_weight'] for it in r.data['items']))
