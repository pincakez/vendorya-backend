"""s176 (Yakot 10-15/16): every optional module has a shop switch; sudo decides which modules a plan / a shop may use.

core/modules.py holds the keys and the rules. These tests pin: what "allowed" means (plan list + per-shop allow / block,
owner level riding on the Treasury), the shop being refused a module it isn't allowed, sudo taking a module away
(switched off, nothing deleted, logged), and the four new switches each stopping NEW records only."""
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from billing.models import Subscription, SubscriptionPlan
from core import modules
from core.models import ActivityLog, Address, Branch, Store, StoreSettings
from inventory.models import Product, ProductVariant, Supplier
from users.models import User


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class _Base(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='md_own', password='x', role='OWNER')
        self.store = Store.objects.create(name='Mods', store_code='761', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.settings, _ = StoreSettings.objects.get_or_create(store=self.store)
        self.sudo = User.objects.create_user(username='md_sudo', password='x', is_superadmin=True)
        self.shop = _client(self.owner)
        self.admin = _client(self.sudo)

    def _plan(self, mods, name='Starter'):
        return SubscriptionPlan.objects.create(name=name, modules=mods)

    def _subscribe(self, plan):
        return Subscription.objects.create(store=self.store, plan=plan)


class AllowedTests(_Base):
    def test_no_plan_means_every_module(self):
        self.assertTrue(all(modules.allowed(self.store).values()))

    def test_plan_list_and_shop_exceptions(self):
        self._subscribe(self._plan(['cheques', 'treasury']))
        ok = modules.allowed(self.store)
        self.assertTrue(ok['cheques'] and ok['treasury'])
        self.assertFalse(ok['services'] or ok['owner_level'] or ok['shop_export'])
        self.store.module_access = {'services': 'allow', 'cheques': 'block'}
        self.store.save()
        ok = modules.allowed(self.store)
        self.assertTrue(ok['services'])
        self.assertFalse(ok['cheques'])

    def test_owner_level_rides_on_the_treasury(self):
        self.store.module_access = {'treasury': 'block', 'owner_level': 'allow'}
        self.store.save()
        self.assertFalse(modules.allowed(self.store)['owner_level'])

    def test_settings_tell_the_shop_what_it_may_use(self):
        self.store.module_access = {'cheques': 'block'}
        self.store.save()
        r = self.shop.get('/api/core/settings/')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data['modules_allowed']['cheques'])
        self.assertTrue(r.data['modules_allowed']['services'])
        self.assertTrue(r.data['services_enabled'])   # existing modules stay ON by default


class ShopSwitchTests(_Base):
    def test_a_blocked_module_cannot_be_switched_on(self):
        self.store.module_access = {'cheques': 'block'}
        self.store.save()
        r = self.shop.patch('/api/core/settings/', {'cheques_enabled': True}, format='json')
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(r.data['code'], 'module_not_allowed')
        self.settings.refresh_from_db()
        self.assertFalse(self.settings.cheques_enabled)
        # switching it OFF (or other modules) is never refused
        r = self.shop.patch('/api/core/settings/', {'services_enabled': False}, format='json')
        self.assertEqual(r.status_code, 200, r.content)

    def test_the_four_new_switches_save(self):
        r = self.shop.patch('/api/core/settings/', {'services_enabled': False, 'supplier_returns_enabled': False,
                                                    'serials_enabled': False, 'transfers_enabled': False}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.settings.refresh_from_db()
        self.assertFalse(self.settings.services_enabled or self.settings.supplier_returns_enabled
                         or self.settings.serials_enabled or self.settings.transfers_enabled)


class SudoControlTests(_Base):
    def test_blocking_a_shop_module_switches_it_off_and_logs_it(self):
        StoreSettings.objects.filter(pk=self.settings.pk).update(cheques_enabled=True)
        r = self.admin.patch(f'/api/admin/stores/{self.store.id}/', {'module_access': {'cheques': 'block'}}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.data['modules_allowed']['cheques'])
        self.settings.refresh_from_db()
        self.assertFalse(self.settings.cheques_enabled)
        self.assertTrue(ActivityLog.all_objects.filter(store=self.store, action__contains='cheques').exists())
        # 'plan' drops the exception again
        r = self.admin.patch(f'/api/admin/stores/{self.store.id}/', {'module_access': {'cheques': 'plan'}}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.store.refresh_from_db()
        self.assertEqual(self.store.module_access, {})

    def test_unknown_keys_and_values_are_refused(self):
        for bad in ({'rockets': 'block'}, {'cheques': 'maybe'}):
            r = self.admin.patch(f'/api/admin/stores/{self.store.id}/', {'module_access': bad}, format='json')
            self.assertEqual(r.status_code, 400, r.content)
        plan = self._plan(None)
        r = self.admin.patch(f'/api/admin/billing/plans/{plan.id}/', {'modules': ['rockets']}, format='json')
        self.assertEqual(r.status_code, 400, r.content)

    def test_shop_cannot_write_its_own_access(self):
        r = self.shop.patch('/api/core/settings/', {'module_access': {'cheques': 'allow'}}, format='json')
        self.store.refresh_from_db()
        self.assertEqual(self.store.module_access, {})
        r = self.shop.patch(f'/api/admin/stores/{self.store.id}/', {'module_access': {}}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_narrowing_a_plan_switches_its_shops_off(self):
        plan = self._plan(None)
        self._subscribe(plan)
        StoreSettings.objects.filter(pk=self.settings.pk).update(reservations_enabled=True, cheques_enabled=True)
        r = self.admin.patch(f'/api/admin/billing/plans/{plan.id}/', {'modules': ['cheques']}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.settings.refresh_from_db()
        self.assertFalse(self.settings.reservations_enabled)
        self.assertTrue(self.settings.cheques_enabled)

    def test_moving_a_shop_to_a_smaller_plan_switches_off(self):
        big, small = self._plan(None, 'Big'), self._plan([], 'Small')
        sub = self._subscribe(big)
        StoreSettings.objects.filter(pk=self.settings.pk).update(cheques_enabled=True)
        r = self.admin.patch(f'/api/admin/billing/subscriptions/{sub.id}/', {'plan': str(small.id)}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.settings.refresh_from_db()
        self.assertFalse(self.settings.cheques_enabled or self.settings.services_enabled)

    def test_treasury_is_not_taken_while_a_drawer_shift_runs(self):
        from finance.models import WorkShift
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        StoreSettings.objects.filter(pk=self.settings.pk).update(treasury_enabled=True)
        WorkShift.all_objects.create(store=self.store, branch=branch, user=self.owner, status=WorkShift.Status.OPEN,
                                     ledger=WorkShift.Ledger.BOX, starting_cash=Decimal('0'))
        r = self.admin.patch(f'/api/admin/stores/{self.store.id}/', {'module_access': {'treasury': 'block'}}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.store.refresh_from_db()
        self.settings.refresh_from_db()
        self.assertEqual(self.store.module_access, {})     # nothing saved
        self.assertTrue(self.settings.treasury_enabled)

    def test_export_can_be_taken_away(self):
        self.store.module_access = {'shop_export': 'block'}
        self.store.save()
        r = self.shop.get('/api/core/shop-export/')
        self.assertEqual(r.status_code, 403)


class NewSwitchGateTests(_Base):
    def setUp(self):
        super().setUp()
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        addr2 = Address.objects.create(store=self.store, street_1='2', city='Cairo')
        self.branch2 = Branch.objects.create(store=self.store, name='Mall', address=addr2)
        self.supplier = Supplier.objects.create(store=self.store, name='Dist', code_prefix='432', prefix_locked=True)
        self.product = Product.objects.create(store=self.store, name='Phone', supplier=self.supplier)
        self.variant = ProductVariant.objects.create(product=self.product, sell_price=Decimal('100'))

    def _off(self, field):
        StoreSettings.objects.filter(pk=self.settings.pk).update(**{field: False})

    def test_service_desk_off_refuses_a_new_job(self):
        self._off('services_enabled')
        r = self.shop.post('/api/services/', {'customer_name': 'A', 'device': 'Laptop'}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data.get('code'), 'module_off')

    def test_transfers_off_refuses_a_new_transfer(self):
        self._off('transfers_enabled')
        r = self.shop.post('/api/inventory/transfers/', {
            'from_branch': str(self.branch.id), 'to_branch': str(self.branch2.id),
            'items': [{'variant': str(self.variant.id), 'quantity': '1'}]}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data.get('code'), 'module_off')

    def test_supplier_returns_off_refuses_a_new_return(self):
        self._off('supplier_returns_enabled')
        r = self.shop.post('/api/finance/purchase-returns/', {'supplier': str(self.supplier.id), 'lines': []}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data.get('code'), 'module_off')

    def test_serials_off_stops_new_tracking_only(self):
        self._off('serials_enabled')
        r = self.shop.patch(f'/api/inventory/products/{self.product.id}/', {'track_serials': True}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('serials_off', str(r.data.get('serial_code')))
        # a product that already tracks serials keeps working (an edit doesn't trip the switch)
        tracked = Product.objects.create(store=self.store, name='Laptop', supplier=self.supplier, track_serials=True)
        ProductVariant.objects.create(product=tracked, sell_price=Decimal('100'))
        r = self.shop.patch(f'/api/inventory/products/{tracked.id}/', {'name': 'Laptop 2'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)

    def test_switch_on_lets_them_through_again(self):
        r = self.shop.patch(f'/api/inventory/products/{self.product.id}/', {'track_serials': True}, format='json')
        self.assertNotIn('serials_off', str(r.data))
