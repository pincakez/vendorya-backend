"""s174 (Yakot: "not optional") — every shop OWNER logs in with an authenticator. Login hands an owner without one a
pre-auth token that only opens the setup screens; setup finishes the login; a saved session of an owner without one
stops renewing; staff stay opt-in."""
from django.conf import settings as dj_settings
from django.core.cache import cache
from django.test import TestCase
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store
from users.models import User

LOGIN = '/api/auth/token/'


def _code(device, offset=0):
    t = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    t.time = t.time + offset * device.step
    return str(t.token()).zfill(device.digits)


class OwnerMustUseAuthenticatorTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        cache.clear()
        self.owner = User.objects.create_user(username='o2fa', password='Pass-word-123', role='OWNER')
        self.store = Store.objects.create(name='Shop 2FA', store_code='2FA', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.cashier = User.objects.create_user(username='c2fa', password='Pass-word-123', role='CASHIER', store=self.store)
        self.c = APIClient()

    def _login(self, user, **extra):
        return self.c.post(LOGIN, {'username': user.username, 'password': 'Pass-word-123', **extra}, format='json')

    def test_staff_still_log_in_without_one(self):
        r = self._login(self.cashier)
        self.assertEqual(r.status_code, 200)
        self.assertIn('access', r.data)

    def test_owner_gets_only_a_setup_pass_then_setup_finishes_the_login(self):
        r = self._login(self.owner)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['requires_2fa_setup'])
        self.assertNotIn('access', r.data)
        pre = r.data['pre_auth_token']

        # the pass opens nothing but the setup screens
        api = APIClient(); api.credentials(HTTP_AUTHORIZATION='Bearer ' + pre)
        self.assertEqual(api.get('/api/auth/me/').status_code, 401)

        self.assertEqual(api.post('/api/auth/2fa/setup/').status_code, 200)
        device = TOTPDevice.objects.get(user=self.owner, confirmed=False)
        r = api.post('/api/auth/2fa/verify-setup/', {'token': _code(device)}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(r.data['backup_codes']), 10)
        self.assertIn('access', r.data)                              # logged in straight away
        me = APIClient(); me.credentials(HTTP_AUTHORIZATION='Bearer ' + r.data['access'])
        self.assertEqual(me.get('/api/auth/me/').status_code, 200)

        # next login: password, then the code
        r = self._login(self.owner)
        self.assertTrue(r.data.get('requires_2fa'))
        r = self._login(self.owner, otp_token=_code(device, offset=1))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn('access', r.data)

    def test_owner_cannot_switch_it_off(self):
        TOTPDevice.objects.create(user=self.owner, name='default', confirmed=True)
        api = APIClient(); api.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.owner).access_token))
        r = api.post('/api/auth/2fa/disable/', {'password': 'Pass-word-123'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertTrue(TOTPDevice.objects.filter(user=self.owner).exists())

    def test_saved_session_without_one_stops_renewing(self):
        refresh = str(RefreshToken.for_user(self.owner))
        r = self.c.post('/api/auth/token/refresh/', {'refresh': refresh}, format='json')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.data['code'], 'twofa_setup_required')
        # …but it renews for an owner who has one, and for staff
        TOTPDevice.objects.create(user=self.owner, name='default', confirmed=True)
        self.assertEqual(self.c.post('/api/auth/token/refresh/', {'refresh': str(RefreshToken.for_user(self.owner))},
                                     format='json').status_code, 200)
        self.assertEqual(self.c.post('/api/auth/token/refresh/', {'refresh': str(RefreshToken.for_user(self.cashier))},
                                     format='json').status_code, 200)

    def test_logout_answers(self):
        # s174: the cookie-clearing helper crashed (delete_cookie got `secure=`) — logout must answer, not 500
        r = self.c.post('/api/auth/logout/', {'refresh': str(RefreshToken.for_user(self.cashier))}, format='json')
        self.assertLess(r.status_code, 500)
