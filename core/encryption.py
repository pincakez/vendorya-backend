"""Transparent field-level encryption at rest (Fernet / AES-128-CBC + HMAC).

Why: secrets like the Gemini API key were stored as plaintext columns, and we
commit DB snapshots to a (private) git repo (`backups/db_*.sql`) — so a leaked
dump would expose the key. `EncryptedCharField` keeps the Python value in
plaintext (every existing `.gemini_api_key` read/write is unchanged) while the
database stores ciphertext.

Key material: a dedicated `FIELD_ENCRYPTION_KEY` setting if present, otherwise
derived from `SECRET_KEY`. The default means prod works with no new env var —
but if you rotate SECRET_KEY without setting FIELD_ENCRYPTION_KEY first, stored
secrets become undecryptable (handled gracefully: the raw value is returned and
the secret must be re-entered). For long-term safety set FIELD_ENCRYPTION_KEY
in `.env` to a stable, independent value.
"""
import base64
import hashlib
from functools import lru_cache

from django.conf import settings
from django.db import models
from cryptography.fernet import Fernet, InvalidToken


@lru_cache(maxsize=1)
def _fernet():
    raw = getattr(settings, 'FIELD_ENCRYPTION_KEY', None) or settings.SECRET_KEY
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
