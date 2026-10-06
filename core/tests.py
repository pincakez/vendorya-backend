from django.conf import settings as dj_settings
from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store
from users.models import User

PIN_URL = '/api/core/lockscreen/pin/'


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class LockscreenPinTests(TestCase):
    """§AUDIT B5 (s156): only the Owner may set/clear the shop's lock-screen PIN (like the rest of the
    lock-screen settings), and PIN guessing is capped."""

    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        cache.clear()
        self.owner = User.objects.create_user(username='own', password='owner-pass-123', role='OWNER')
        self.store = Store.objects.create(name='Acme', store_code='ACM', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.cashier = User.objects.create_user(username='csh', password='cash-pass-123', role='CASHIER', store=self.store)
        self.admin = User.objects.create_user(username='adm', password='adm-pass-123', role='ADMIN', store=self.store)
        s = self.store.settings
        s.lock_pin_hash = make_password('1234')
        s.save(update_fields=['lock_pin_hash'])

    def _pin_is(self, pin):
        from django.contrib.auth.hashers import check_password
        self.store.settings.refresh_from_db()
        return check_password(pin, self.store.settings.lock_pin_hash)

    def test_cashier_cannot_change_or_clear_pin(self):
        c = _client(self.cashier)
        r = c.post(PIN_URL, {'action': 'set', 'new_pin': '9999', 'password': 'cash-pass-123'}, format='json')
        self.assertEqual(r.status_code, 403)
        r = c.post(PIN_URL, {'action': 'clear', 'password': 'cash-pass-123'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self._pin_is('1234'))

    def test_admin_cannot_change_pin(self):
        r = _client(self.admin).post(PIN_URL, {'action': 'set', 'new_pin': '9999', 'password': 'adm-pass-123'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self._pin_is('1234'))

    def test_owner_can_change_pin(self):
        r = _client(self.owner).post(PIN_URL, {'action': 'set', 'new_pin': '5678', 'old_pin': '1234'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(self._pin_is('5678'))

    def test_cashier_can_verify(self):
        r = _client(self.cashier).post(PIN_URL, {'action': 'verify', 'pin': '1234'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['valid'])

    def test_verify_locks_after_5_wrong_tries(self):
        c = _client(self.cashier)
        for _ in range(5):
            r = c.post(PIN_URL, {'action': 'verify', 'pin': '0000'}, format='json')
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.data['valid'])
        # 6th try — even the RIGHT pin is refused while locked
        r = c.post(PIN_URL, {'action': 'verify', 'pin': '1234'}, format='json')
        self.assertEqual(r.status_code, 429)
        self.assertFalse(r.data['valid'])

    def test_right_pin_resets_the_counter(self):
        c = _client(self.cashier)
        for _ in range(4):
            c.post(PIN_URL, {'action': 'verify', 'pin': '0000'}, format='json')
        self.assertTrue(c.post(PIN_URL, {'action': 'verify', 'pin': '1234'}, format='json').data['valid'])
        for _ in range(4):
            c.post(PIN_URL, {'action': 'verify', 'pin': '0000'}, format='json')
        self.assertEqual(c.post(PIN_URL, {'action': 'verify', 'pin': '1234'}, format='json').status_code, 200)


class CashierSettingsTests(TestCase):
    """s156 (found by the till browser test): GET /api/core/settings/ was Manager+, so a cashier's till ran on
    defaults — and the lock screen, seeing no `lock_pin_set`, unlocked WITHOUT the PIN. Cashiers may read the
    settings; the login IP allow-list stays Manager+."""

    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_c', password='x', role='OWNER')
        self.store = Store.objects.create(name='C1', store_code='105', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.cashier = User.objects.create_user(username='csh_c', password='x', role='CASHIER', store=self.store)
        s = self.store.settings
        s.lock_pin_hash = make_password('1234')
        s.save(update_fields=['lock_pin_hash'])

    def test_cashier_reads_settings_without_ip_allowlist(self):
        r = _client(self.cashier).get('/api/core/settings/')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['lock_pin_set'])
        self.assertNotIn('login_ip_allowlist', r.data)

    def test_owner_still_sees_ip_allowlist(self):
        self.assertIn('login_ip_allowlist', _client(self.owner).get('/api/core/settings/').data)

    def test_cashier_cannot_change_settings(self):
        self.assertEqual(_client(self.cashier).patch('/api/core/settings/', {'decimals': 3}, format='json').status_code, 403)


class AdminStoreUsageTests(TestCase):
    """s157: Admin → Usage crashed (500) — it summed a field `total` that does not exist on SalesInvoice.
    Revenue this month = POSTED invoices' grand_total only (drafts and voids are not revenue)."""

    def setUp(self):
        from decimal import Decimal
        from django.utils import timezone
        from core.models import Address, Branch
        from users.models import Customer
        from finance.models import SalesInvoice
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_u', password='x', role='OWNER')
        self.store = Store.objects.create(name='Usage Co', store_code='USG', owner=self.owner)
        self.sudo = User.objects.create_user(username='sudo_u', password='x', is_superadmin=True)
        branch = Branch.objects.create(store=self.store, name='Main',
                                       address=Address.objects.create(store=self.store, street_1='1', city='Cairo'))
        cust = Customer.objects.create(store=self.store, name='Buyer', phone_number='0100')
        for st, amt in (('POSTED', '150'), ('POSTED', '50'), ('DRAFT', '999'), ('VOID', '777')):
            SalesInvoice.objects.create(store=self.store, branch=branch, customer=cust, status=st,
                                        date=timezone.now(), grand_total=Decimal(amt))

    def test_usage_answers_with_posted_revenue(self):
        r = _client(self.sudo).get(f'/api/admin/stores/{self.store.pk}/usage/')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(float(r.data['revenue_month']), 200.0)


class ItemNounTests(TestCase):
    """§AUDIT B6 (s157): Settings → "Items are called" is free text (letters, digits, "-", max 10), as the
    screen allows — the server only took NAME/PRODUCT/ITEM/MODEL, so e.g. "Device" was refused."""

    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_n', password='x', role='OWNER')
        self.store = Store.objects.create(name='N1', store_code='106', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])

    def _patch(self, word):
        return _client(self.owner).patch('/api/core/settings/', {'item_noun': word}, format='json')

    def test_free_word_is_saved(self):
        r = self._patch('Device-2')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['item_noun'], 'Device-2')

    def test_old_fixed_values_still_work(self):
        self.assertEqual(self._patch('MODEL').status_code, 200)

    def test_bad_words_refused(self):
        for bad in ('', 'two words', 'Way-too-long-1', 'x<y>'):
            self.assertEqual(self._patch(bad).status_code, 400, bad)
