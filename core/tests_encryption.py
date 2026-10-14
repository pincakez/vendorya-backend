import os

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from core import encryption
from core.encryption import EncryptedCharField


class FieldEncryptionKeyTests(SimpleTestCase):
    """§C9.8 (s165): FIELD_ENCRYPTION_KEY is really loaded from the environment, is independent of
    SECRET_KEY, and a missing key outside DEBUG is an error — never a silent SECRET_KEY fallback."""

    def setUp(self):
        encryption._fernet.cache_clear()
        self.addCleanup(encryption._fernet.cache_clear)
        self.field = EncryptedCharField(max_length=500)

    def _roundtrip(self, plain):
        stored = self.field.get_prep_value(plain)
        return stored, self.field.from_db_value(stored, None, None)

    def test_settings_load_the_key_from_the_environment(self):
        self.assertEqual(settings.FIELD_ENCRYPTION_KEY, os.environ.get('FIELD_ENCRYPTION_KEY', '').strip())

    @override_settings(FIELD_ENCRYPTION_KEY='k-one-long-random-string', DEBUG=False)
    def test_roundtrip_with_the_key(self):
        stored, back = self._roundtrip('my secret')
        self.assertTrue(stored.startswith(EncryptedCharField.PREFIX))
        self.assertNotIn('my secret', stored)
        self.assertEqual(back, 'my secret')

    def test_key_is_independent_of_secret_key(self):
        with override_settings(FIELD_ENCRYPTION_KEY='k-one-long-random-string', SECRET_KEY='s1'):
            stored, _ = self._roundtrip('my secret')
        encryption._fernet.cache_clear()
        with override_settings(FIELD_ENCRYPTION_KEY='k-one-long-random-string', SECRET_KEY='s2-rotated'):
            self.assertEqual(self.field.from_db_value(stored, None, None), 'my secret')
        encryption._fernet.cache_clear()
        with override_settings(FIELD_ENCRYPTION_KEY='a-different-key'):
            self.assertEqual(self.field.from_db_value(stored, None, None), stored)   # unreadable → raw, no 500

    @override_settings(FIELD_ENCRYPTION_KEY='', DEBUG=False)
    def test_missing_key_in_prod_is_an_error(self):
        with self.assertRaises(ImproperlyConfigured):
            self.field.get_prep_value('my secret')

    @override_settings(FIELD_ENCRYPTION_KEY='', DEBUG=True)
    def test_missing_key_in_dev_falls_back(self):
        _, back = self._roundtrip('my secret')
        self.assertEqual(back, 'my secret')

    @override_settings(FIELD_ENCRYPTION_KEY='', DEBUG=False)
    def test_empty_values_never_need_the_key(self):
        self.assertEqual(self.field.get_prep_value(''), '')
        self.assertIsNone(self.field.from_db_value(None, None, None))
