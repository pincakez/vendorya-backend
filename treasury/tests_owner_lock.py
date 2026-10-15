"""s174 — My money behind the owner's authenticator (owner_lock.py): the server keeps the lock, the code opens it for the
shop's minutes, one login token at a time; the switch itself can't be turned on without an authenticator, nor off while locked."""
from datetime import timedelta
from unittest import mock

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from core.models import StoreSettings
from users.models import User

from .tests_owner_api import API, _client, _shop

LOCK = API + 'lock/'
SETTINGS = '/api/core/settings/'


class OwnerLockTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        cache.clear()
        self.user = User.objects.create_user(username='ol_own', password='x', role='OWNER')
        self.store, _ = _shop('601', self.user)
        self.second_owner = User.objects.create_user(username='ol_own2', password='x', role='OWNER', store=self.store)
        self.c = _client(self.user)

    def _enroll(self, codes=('11111111', '22222222', '33333333')):
        TOTPDevice.objects.create(user=self.user, name='phone', confirmed=True)
        static = StaticDevice.objects.create(user=self.user, name='backup', confirmed=True)
        for code in codes:
            StaticToken.objects.create(device=static, token=code)

    def _lock_on(self, minutes=15):
        self._enroll()
        r = self.c.patch(SETTINGS, {'owner_lock_enabled': True, 'owner_unlock_minutes': minutes}, format='json')
        self.assertEqual(r.status_code, 200, r.content)

    def test_off_by_default_nothing_changes(self):
        self.assertEqual(self.c.get(API + 'overview/').status_code, 200)
        self.assertEqual(self.c.get(LOCK).data['enabled'], False)
        self.assertTrue(self.c.get(LOCK).data['open'])

    def test_cannot_turn_on_without_an_authenticator(self):
        r = self.c.patch(SETTINGS, {'owner_lock_enabled': True}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['code'], 'authenticator_needed')
        self.assertFalse(StoreSettings.objects.get(store=self.store).owner_lock_enabled)

    def test_only_the_owner_person_changes_it(self):
        r = _client(self.second_owner).patch(SETTINGS, {'owner_lock_enabled': True}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['code'], 'not_owner')

    def test_locked_until_the_code_then_open_for_this_login_only(self):
        self._lock_on()
        r = self.c.get(API + 'overview/')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['code'], 'owner_locked')
        state = self.c.get(LOCK).data
        self.assertEqual((state['enabled'], state['open'], state['enrolled']), (True, False, True))

        r = self.c.post(LOCK + 'open/', {'code': '99999999'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['code'], 'wrong_code')
        self.assertEqual(self.c.get(API + 'overview/').status_code, 403)
        # django-otp makes the next try wait 1 s after a wrong code (then 2, 4 …) — a person retyping is slower than that
        StaticDevice.objects.update(throttling_failure_count=0, throttling_failure_timestamp=None)

        r = self.c.post(LOCK + 'open/', {'code': '11111111'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.data['open'])
        self.assertTrue(0 < r.data['seconds_left'] <= 15 * 60)
        self.assertEqual(self.c.get(API + 'overview/').status_code, 200)
        # another login (another device / token) of the same owner stays locked
        self.assertEqual(_client(self.user).get(API + 'overview/').status_code, 403)
        # a backup code works once
        self.assertEqual(_client(self.user).post(LOCK + 'open/', {'code': '11111111'}, format='json').status_code, 400)

        self.c.post(LOCK + 'close/')
        self.assertEqual(self.c.get(API + 'overview/').status_code, 403)

    def test_closes_by_itself_after_the_minutes(self):
        self._lock_on(minutes=5)
        self.c.post(LOCK + 'open/', {'code': '22222222'}, format='json')
        self.assertEqual(self.c.get(API + 'overview/').status_code, 200)
        later = timezone.now() + timedelta(minutes=6)
        with mock.patch('treasury.owner_lock.timezone.now', return_value=later):
            self.assertEqual(self.c.get(API + 'overview/').status_code, 403)

    def test_five_wrong_codes_pause_even_the_right_one(self):
        self._lock_on()
        for _ in range(5):
            self.c.post(LOCK + 'open/', {'code': '00000000'}, format='json')
        r = self.c.post(LOCK + 'open/', {'code': '33333333'}, format='json')
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.c.get(API + 'overview/').status_code, 403)

    def test_switching_off_or_new_minutes_needs_it_open(self):
        self._lock_on()
        r = self.c.patch(SETTINGS, {'owner_lock_enabled': False}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['code'], 'owner_locked')
        r = self.c.patch(SETTINGS, {'owner_unlock_minutes': 240}, format='json')
        self.assertEqual(r.status_code, 403)
        # the whole Capabilities form sends unchanged values — that must still save
        self.assertEqual(self.c.patch(SETTINGS, {'owner_lock_enabled': True, 'owner_unlock_minutes': 15,
                                                 'reservations_enabled': True}, format='json').status_code, 200)
        self.c.post(LOCK + 'open/', {'code': '22222222'}, format='json')
        self.assertEqual(self.c.patch(SETTINGS, {'owner_lock_enabled': False}, format='json').status_code, 200)
        self.assertEqual(self.c.get(API + 'overview/').status_code, 200)

    def test_minutes_between_1_and_240(self):
        self._enroll()
        self.assertEqual(self.c.patch(SETTINGS, {'owner_unlock_minutes': 0}, format='json').status_code, 400)
        self.assertEqual(self.c.patch(SETTINGS, {'owner_unlock_minutes': 241}, format='json').status_code, 400)
