from django.conf import settings as dj_settings
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store
from users.models import User


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class StaffRankTests(TestCase):
    """§AUDIT B4 (s156): nobody may create, promote to, or touch an account at or above their own rank —
    so an Admin can't make an Owner, promote themselves, or lock out / re-password the Owner."""

    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own', password='x', role='OWNER')
        self.store = Store.objects.create(name='Acme', store_code='ACM', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        mk = lambda u, r: User.objects.create_user(username=u, password='x', role=r, store=self.store)
        self.admin, self.admin2 = mk('adm', 'ADMIN'), mk('adm2', 'ADMIN')
        self.manager, self.cashier = mk('mgr', 'MANAGER'), mk('csh', 'CASHIER')

    def _new(self, client, role, **extra):
        return client.post('/api/auth/staff/', {'username': f'new_{role.lower()}', 'first_name': 'N',
                                                'role': role, **extra}, format='json')

    # ── create ───────────────────────────────────────────────────────────
    def test_admin_cannot_create_owner_or_admin(self):
        c = _client(self.admin)
        self.assertEqual(self._new(c, 'OWNER').status_code, 403)
        self.assertEqual(self._new(c, 'ADMIN').status_code, 403)
        self.assertFalse(User.objects.filter(username__startswith='new_').exists())

    def test_admin_can_create_manager_and_cashier(self):
        c = _client(self.admin)
        self.assertEqual(self._new(c, 'MANAGER').status_code, 201)
        self.assertEqual(self._new(c, 'CASHIER').status_code, 201)

    def test_owner_can_create_admin_but_not_owner(self):
        c = _client(self.owner)
        self.assertEqual(self._new(c, 'ADMIN').status_code, 201)
        self.assertEqual(self._new(c, 'OWNER').status_code, 403)

    def test_create_without_password_generates_one(self):
        r = self._new(_client(self.owner), 'CASHIER')
        self.assertEqual(r.status_code, 201, r.content)
        u = User.objects.get(username='new_cashier')
        self.assertTrue(u.has_usable_password())

    # ── edit others ──────────────────────────────────────────────────────
    def test_admin_cannot_touch_owner(self):
        c = _client(self.admin)
        url = f'/api/auth/staff/{self.owner.id}/'
        self.assertEqual(c.patch(url, {'is_active': False}, format='json').status_code, 403)
        self.assertEqual(c.patch(url, {'password': 'Hijacked-Pass-123'}, format='json').status_code, 403)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertTrue(self.owner.check_password('x'))

    def test_admin_cannot_touch_other_admin(self):
        r = _client(self.admin).patch(f'/api/auth/staff/{self.admin2.id}/', {'is_active': False}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_admin_can_edit_cashier_but_not_promote_to_admin(self):
        c = _client(self.admin)
        url = f'/api/auth/staff/{self.cashier.id}/'
        self.assertEqual(c.patch(url, {'first_name': 'Cash'}, format='json').status_code, 200)
        self.assertEqual(c.patch(url, {'role': 'MANAGER'}, format='json').status_code, 200)
        self.assertEqual(c.patch(url, {'role': 'ADMIN'}, format='json').status_code, 403)

    def test_owner_can_edit_admin(self):
        r = _client(self.owner).patch(f'/api/auth/staff/{self.admin.id}/', {'is_active': False}, format='json')
        self.assertEqual(r.status_code, 200)

    # ── edit self ────────────────────────────────────────────────────────
    def test_admin_cannot_promote_or_deactivate_self(self):
        c = _client(self.admin)
        url = f'/api/auth/staff/{self.admin.id}/'
        self.assertEqual(c.patch(url, {'role': 'OWNER'}, format='json').status_code, 403)
        self.assertEqual(c.patch(url, {'is_active': False}, format='json').status_code, 403)
        self.assertEqual(c.patch(url, {'first_name': 'Me'}, format='json').status_code, 200)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, 'ADMIN')
