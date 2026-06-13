from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from core.models import Store
from users.models import User
from .models import APIKey, _hash_raw
from .authentication import APIKeyAuthentication
from .scopes import grants, normalize_scopes, required_scope, is_valid_scope


def make_store(name, code, username):
    """Create an owner user + store (Store.owner is required), wired both ways."""
    owner = User.objects.create_user(username=username, password='x', role='OWNER')
    store = Store.objects.create(name=name, store_code=code, owner=owner)
    owner.store = store
    owner.save(update_fields=['store'])
    return store, owner


class APIKeyModelTests(TestCase):
    def setUp(self):
        self.store, self.user = make_store('Acme', 'ACM', 'owner')

    def test_generate_returns_raw_once_and_stores_only_hash(self):
        key, raw = APIKey.generate(store=self.store, label='Test', created_by=self.user,
                                   scopes=['inventory:read'])
        self.assertTrue(raw.startswith('vdy_'))
        self.assertEqual(key.key_hash, _hash_raw(raw))
        self.assertNotIn(raw.split('_', 2)[2], key.key_hash)   # secret half not in stored hash
        self.assertEqual(key.scopes, ['inventory:read'])

    def test_resolve_valid_and_tampered(self):
        key, raw = APIKey.generate(store=self.store, label='K', created_by=self.user)
        self.assertEqual(APIKey.resolve(raw), key)
        self.assertIsNone(APIKey.resolve(raw + 'tamper'))
        self.assertIsNone(APIKey.resolve('garbage'))
        self.assertIsNone(APIKey.resolve(''))

    def test_resolve_rejects_revoked_and_expired(self):
        key, raw = APIKey.generate(store=self.store, label='K', created_by=self.user)
        key.revoke()
        self.assertIsNone(APIKey.resolve(raw))

        key2, raw2 = APIKey.generate(store=self.store, label='K2', created_by=self.user,
                                     expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(APIKey.resolve(raw2))

    def test_scopes_normalized_on_generate(self):
        key, _ = APIKey.generate(store=self.store, label='K', created_by=self.user,
                                 scopes=['inventory:read', 'inventory:read', 'BOGUS:read', 'sales:write'])
        self.assertEqual(key.scopes, ['inventory:read', 'sales:write'])


class ScopeLogicTests(TestCase):
    def test_write_implies_read(self):
        held = ['inventory:write']
        self.assertTrue(grants(held, 'inventory:read'))
        self.assertTrue(grants(held, 'inventory:write'))
        self.assertFalse(grants(held, 'sales:read'))

    def test_required_scope_by_method(self):
        self.assertEqual(required_scope('sales', 'GET'), 'sales:read')
        self.assertEqual(required_scope('sales', 'POST'), 'sales:write')
        self.assertEqual(required_scope('sales', 'DELETE'), 'sales:write')

    def test_validation_and_normalize(self):
        self.assertTrue(is_valid_scope('finance:write'))
        self.assertFalse(is_valid_scope('finance:admin'))
        self.assertFalse(is_valid_scope('nope'))
        self.assertEqual(normalize_scopes([' Inventory:Read ', 'x']), ['inventory:read'])


class APIKeyAuthTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.auth = APIKeyAuthentication()
        self.store, self.user = make_store('Acme', 'ACM', 'owner')

    def _req(self, header_value=None, x_api_key=None):
        kw = {}
        if header_value is not None:
            kw['HTTP_AUTHORIZATION'] = header_value
        if x_api_key is not None:
            kw['HTTP_X_API_KEY'] = x_api_key
        return self.factory.get('/api/anything/', **kw)

    def test_no_key_returns_none(self):
        self.assertIsNone(self.auth.authenticate(self._req()))

    def test_authorization_header_success_binds_store(self):
        key, raw = APIKey.generate(store=self.store, label='K', created_by=self.user)
        user, api_key = self.auth.authenticate(self._req(header_value=f'Api-Key {raw}'))
        self.assertEqual(user, self.user)
        self.assertEqual(user.store, self.store)
        self.assertEqual(api_key, key)

    def test_x_api_key_header_success(self):
        key, raw = APIKey.generate(store=self.store, label='K', created_by=self.user)
        user, api_key = self.auth.authenticate(self._req(x_api_key=raw))
        self.assertEqual(api_key, key)

    def test_invalid_key_raises(self):
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(self._req(x_api_key='vdy_dead_beef'))

    def test_inactive_owner_rejected(self):
        key, raw = APIKey.generate(store=self.store, label='K', created_by=self.user)
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(self._req(x_api_key=raw))

    def test_last_used_recorded(self):
        key, raw = APIKey.generate(store=self.store, label='K', created_by=self.user)
        self.assertIsNone(key.last_used_at)
        self.auth.authenticate(self._req(x_api_key=raw))
        key.refresh_from_db()
        self.assertIsNotNone(key.last_used_at)


class APIKeyTenantIsolationTests(TestCase):
    """A key only ever sees its own store's keys via the scoped manager."""
    def test_scoped_manager_isolates_keys(self):
        a, ua = make_store('A', 'AAA', 'ua')
        b, ub = make_store('B', 'BBB', 'ub')
        APIKey.generate(store=a, label='ka', created_by=ua)
        APIKey.generate(store=b, label='kb', created_by=ub)

        from core.tenancy import set_current_store, clear_current_request
        try:
            set_current_store(a)
            self.assertEqual([k.store_id for k in APIKey.objects.all()], [a.id])
            self.assertEqual(APIKey.all_objects.count(), 2)
        finally:
            clear_current_request()
