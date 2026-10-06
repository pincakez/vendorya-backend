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
