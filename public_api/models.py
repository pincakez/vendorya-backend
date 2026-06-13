"""Public API keys (a.k.a. Personal Access Tokens).

Security model:
- The raw key is shown to the user EXACTLY ONCE, at creation. We never store it.
- We store a SHA-256 hash of the raw key (API keys are high-entropy random
  strings, so a fast hash is appropriate — bcrypt is for low-entropy passwords).
- A short, non-secret `key_prefix` is stored in the clear for O(1) lookup and
  so the UI can show "vdy_ab12cd34…" in the key list.
- Each key is bound to one (store, user) and carries its own scope list, so an
  API request inherits the same tenant isolation + permission model as the app.
"""
import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import Store, TimestampedModel
from core.tenancy import TenantScopedManager
from .scopes import normalize_scopes

KEY_NAMESPACE = 'vdy'          # human-recognizable prefix on every key
PREFIX_LEN = 8                 # chars of the public lookup prefix
SECRET_BYTES = 32             # entropy of the secret half (~43 url-safe chars)


def _hash_raw(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


class APIKey(TimestampedModel):
    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store     = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='api_keys')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='created_api_keys')

    label      = models.CharField(_("Label"), max_length=120,
                                  help_text=_("Human name, e.g. 'Zapier integration'."))
    key_prefix = models.CharField(_("Key prefix"), max_length=32, unique=True, db_index=True,
                                  help_text=_("Public, non-secret lookup prefix (shown in the UI)."))
    key_hash   = models.CharField(_("Key hash"), max_length=64,
                                  help_text=_("SHA-256 of the full raw key. The raw key is never stored."))
    scopes     = models.JSONField(_("Scopes"), default=list, blank=True)

    is_active    = models.BooleanField(_("Active"), default=True)
    expires_at   = models.DateTimeField(_("Expires at"), null=True, blank=True,
                                        help_text=_("Null = never expires."))
    last_used_at = models.DateTimeField(_("Last used"), null=True, blank=True)

    objects     = TenantScopedManager()   # secure-by-default: a store sees only its own keys
    all_objects = models.Manager()         # escape hatch (auth lookup, sudo, audit)

    class Meta:
        verbose_name = _("API Key")
        verbose_name_plural = _("API Keys")
        ordering = ['-created_at']
        indexes = [models.Index(fields=['store', '-created_at'])]

    def __str__(self):
        return f"{self.label} ({self.key_prefix}…)"

    # ---------- lifecycle ----------

    @classmethod
    def generate(cls, *, store, label, scopes=None, created_by=None, expires_at=None):
        """Mint a new key. Returns (api_key_instance, raw_key).

        The raw key is returned ONCE and never persisted — surface it to the
        caller immediately, then it's gone.
        """
        prefix = secrets.token_hex(PREFIX_LEN // 2)            # 8 hex chars
        secret = secrets.token_urlsafe(SECRET_BYTES)
        key_prefix = f"{KEY_NAMESPACE}_{prefix}"
        raw_key = f"{key_prefix}_{secret}"
        obj = cls.objects.create(
            store=store,
            created_by=created_by,
            label=label,
            key_prefix=key_prefix,
            key_hash=_hash_raw(raw_key),
            scopes=normalize_scopes(scopes),
            expires_at=expires_at,
        )
        return obj, raw_key

    @staticmethod
    def split_raw(raw_key):
        """Return the (key_prefix) portion of a raw key, or None if malformed."""
        parts = (raw_key or '').split('_')
        if len(parts) < 3 or parts[0] != KEY_NAMESPACE:
            return None
        return f"{parts[0]}_{parts[1]}"

    @classmethod
    def resolve(cls, raw_key):
        """Look up an active, non-expired key matching `raw_key`, else None.

        Constant-time hash comparison; lookup is by public prefix so we never
        scan the whole table.
        """
        key_prefix = cls.split_raw(raw_key)
        if not key_prefix:
            return None
        obj = cls.all_objects.filter(key_prefix=key_prefix, is_active=True).first()
        if obj is None:
            return None
        if not secrets.compare_digest(obj.key_hash, _hash_raw(raw_key)):
            return None
        if obj.is_expired:
            return None
        return obj

    @property
    def is_expired(self):
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_usable(self):
        return self.is_active and not self.is_expired

    def touch(self):
        """Record usage without racing other concurrent requests on the same key."""
        APIKey.all_objects.filter(pk=self.pk).update(last_used_at=timezone.now())

    def revoke(self):
        self.is_active = False
        self.save(update_fields=['is_active', 'updated_at'])
