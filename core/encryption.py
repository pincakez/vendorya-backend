"""Transparent field-level encryption at rest (Fernet / AES-128-CBC + HMAC).

Why: secrets like the Gemini API key were stored as plaintext columns, and we
commit DB snapshots to a (private) git repo (`backups/db_*.sql`) — so a leaked
dump would expose the key. `EncryptedCharField` keeps the Python value in
plaintext (every existing `.gemini_api_key` read/write is unchanged) while the
database stores ciphertext.

Key material: the `FIELD_ENCRYPTION_KEY` setting (env var of the same name,
loaded in settings.py). It must stay stable and independent of SECRET_KEY —
rotating SECRET_KEY must never make stored secrets unreadable.

§C9.8 (s165): settings used to never load the key, so this silently fell back to
SECRET_KEY everywhere. Now:
  * key set            → used.
  * key missing, DEBUG → falls back to SECRET_KEY (dev/test convenience only).
  * key missing, prod  → ImproperlyConfigured the first time something encrypts
                         or decrypts — never a silent SECRET_KEY-derived key.
The check is lazy (on first use), so a box without the key still boots while no
model uses EncryptedCharField (true as of s165 — the Gemini key field was removed
in s152).

Unreadable ciphertext (wrong key) is handled gracefully: the raw value is
returned and the secret must be re-entered.
"""
import base64
import hashlib
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from cryptography.fernet import Fernet, InvalidToken


def _key_material():
    raw = (getattr(settings, 'FIELD_ENCRYPTION_KEY', '') or '').strip()
    if raw:
        return raw
    if settings.DEBUG:
        return settings.SECRET_KEY   # dev/test only — see the module docstring
    raise ImproperlyConfigured(
        'FIELD_ENCRYPTION_KEY is not set. Set it in the environment (.env) to a long random '
        'string, independent of DJANGO_SECRET_KEY, before storing encrypted fields.'
    )


@lru_cache(maxsize=1)
def _fernet():
    raw = _key_material()
    # Fernet needs 32 url-safe base64 bytes; derive deterministically from the key material.
    digest = hashlib.sha256(raw.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class EncryptedCharField(models.CharField):
    """A CharField whose value is encrypted in the database, transparent to code.

    Ciphertext is stored with an `enc::` marker so legacy plaintext rows (written
    before this field existed) are detected and returned as-is — they get
    encrypted automatically the next time the row is saved.
    """
    PREFIX = 'enc::'

    def from_db_value(self, value, expression, connection):
        if not value or not value.startswith(self.PREFIX):
            return value  # empty, or legacy plaintext
        try:
            return _fernet().decrypt(value[len(self.PREFIX):].encode()).decode()
        except InvalidToken:
            # Key rotated / unreadable — return raw rather than 500 the whole app.
            return value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value or value.startswith(self.PREFIX):
            return value  # empty, or already encrypted (idempotent)
        token = _fernet().encrypt(value.encode()).decode()
        return self.PREFIX + token
