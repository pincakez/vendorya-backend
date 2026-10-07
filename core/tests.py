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
    s163 §PRIVACY-SUDO: usage shows activity COUNTS only — the shop's revenue is no longer sent to sudo."""

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

    def test_usage_answers_with_counts_but_no_money(self):
        r = _client(self.sudo).get(f'/api/admin/stores/{self.store.pk}/usage/')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(r.data['invoices_total'], 4)
        self.assertNotIn('revenue_month', r.data)


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


class LabelPresetRoleTests(TestCase):
    """§AUDIT B7 (s157): cashiers print labels, so they may LIST/READ the shop's label presets; only a
    Manager+ may change them. The view's role list said so, but a Manager-only gate ran first."""

    def setUp(self):
        from core.models import LabelPreset
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_l', password='x', role='OWNER')
        self.store = Store.objects.create(name='L1', store_code='107', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.cashier = User.objects.create_user(username='csh_l', password='x', role='CASHIER', store=self.store)
        self.manager = User.objects.create_user(username='mgr_l', password='x', role='MANAGER', store=self.store)
        self.preset = LabelPreset.objects.create(store=self.store, name='Small')

    def test_cashier_can_list_and_read(self):
        c = _client(self.cashier)
        self.assertEqual(c.get('/api/core/label-presets/').status_code, 200)
        self.assertEqual(c.get(f'/api/core/label-presets/{self.preset.pk}/').status_code, 200)

    def test_cashier_cannot_change(self):
        c = _client(self.cashier)
        self.assertEqual(c.post('/api/core/label-presets/', {'name': 'X'}, format='json').status_code, 403)
        self.assertEqual(c.delete(f'/api/core/label-presets/{self.preset.pk}/').status_code, 403)

    def test_manager_can_create(self):
        self.assertEqual(_client(self.manager).post('/api/core/label-presets/', {'name': 'Big'}, format='json').status_code, 201)


class PlatformAccountPrivacyTests(TestCase):
    """§PRIVACY-SUDO (s163, Yakot 2026-10-08): the platform super-admin ("sudo") never sees a shop's business
    data — no "Enter store" (X-Store-ID), never inside a shop, shop endpoints refuse it, and the admin pages
    show counts and kinds of activity, never amounts, names or invoice numbers."""

    SHOP_URLS = ('/api/finance/invoices/', '/api/inventory/products/', '/api/core/dashboard/',
                 '/api/reports/pnl/', '/api/core/store/', '/api/core/settings/', '/api/pos/top-selling/')

    def setUp(self):
        from decimal import Decimal
        from django.utils import timezone
        from core.models import Address, Branch, ActivityLog
        from users.models import Customer
        from finance.models import SalesInvoice
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_p', password='x', role='OWNER')
        self.store = Store.objects.create(name='Private Co', store_code='PRV', owner=self.owner)
        self.owner.store = self.store; self.owner.save()
        branch = Branch.objects.create(store=self.store, name='Main',
                                       address=Address.objects.create(store=self.store, street_1='1', city='Cairo'))
        cust = Customer.objects.create(store=self.store, name='Secret Buyer', phone_number='0100')
        SalesInvoice.objects.create(store=self.store, branch=branch, customer=cust, status='POSTED',
                                          date=timezone.now(), grand_total=Decimal('31000'))
        cust.delete()   # soft delete → lands in the admin trash
        ActivityLog.all_objects.create(store=self.store, user=self.owner, operation_type='OTHER',
                                       action='Sold to Secret Buyer for 31,000', details={'amount': '31000'})
        # even if someone tries to put sudo inside a shop, saving takes it out again
        self.sudo = User.objects.create_user(username='sudo_p', password='x', is_superadmin=True, store=self.store)

    def test_sudo_is_never_inside_a_shop(self):
        self.sudo.refresh_from_db()
        self.assertIsNone(self.sudo.store_id)

    def test_shop_endpoints_refuse_sudo_with_or_without_the_old_header(self):
        c = _client(self.sudo)
        for url in self.SHOP_URLS:
            self.assertEqual(c.get(url).status_code, 403, url)
            self.assertEqual(c.get(url, HTTP_X_STORE_ID=str(self.store.pk)).status_code, 403, url)

    def test_the_owner_still_sees_his_shop(self):
        c = _client(self.owner)
        self.assertEqual(c.get('/api/core/store/').status_code, 200)
        self.assertEqual(c.get('/api/finance/invoices/').status_code, 200)

    def test_tenant_manager_returns_nothing_for_sudo(self):
        from core.tenancy import set_current_request, set_current_store, clear_current_request
        from finance.models import SalesInvoice
        class _Req: pass
        req = _Req(); req.user = self.sudo
        set_current_request(req); set_current_store(None)
        try:
            self.assertEqual(SalesInvoice.objects.count(), 0)
        finally:
            clear_current_request()
        self.assertGreaterEqual(SalesInvoice.all_objects.count(), 1)   # outside a request: unchanged

    def test_admin_activity_log_has_no_action_text_or_details(self):
        rows = _client(self.sudo).get('/api/admin/activity-logs/').data
        rows = rows.get('results', rows) if isinstance(rows, dict) else rows
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn('action', row)
            self.assertNotIn('details', row)
        self.assertNotIn('31,000', str(rows))
        self.assertNotIn('Secret Buyer', str(rows))

    def test_admin_trash_hides_business_names(self):
        r = _client(self.sudo).get(f'/api/admin/trash/?store={self.store.pk}')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('Secret Buyer', str(r.data))

    def test_sudo_export_endpoint_is_gone(self):
        self.assertEqual(_client(self.sudo).get(f'/api/admin/stores/{self.store.pk}/export/').status_code, 404)

    def test_currency_list_still_open_to_sudo(self):
        self.assertEqual(_client(self.sudo).get('/api/core/currencies/').status_code, 200)
