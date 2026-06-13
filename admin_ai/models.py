"""Admin AI data models.

All AI infrastructure is platform-level (sudo-only). No per-tenant scoping
on these tables — when a sudo user "Acts As" a store, that context is
applied at the *tool* layer, not the persistence layer.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from pgvector.django import VectorField, HnswIndex

from core.models import Store, TimestampedModel, SoftDeleteModel
from core.encryption import EncryptedCharField


# ---------- platform-level settings ----------

class AISettings(TimestampedModel):
    """Singleton holding the Gemini API key and other platform-wide AI knobs.

    Enforced single-row via `pk=1` save() override. Edited via the C2 Misc
    page (sudo-only). The key is encrypted at rest (EncryptedCharField) so it
    never appears in plaintext in the DB or the committed `backups/*.sql` dumps;
    the API serializer is already write-only + masked.
    """

    id           = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    # Encrypted at rest; ciphertext is longer than the raw ~40-char key, so 512.
    gemini_api_key = EncryptedCharField(_("Gemini API Key"), max_length=512, blank=True, default='')

    # Safe, code-free knobs for the auto model-discovery (edited on the Misc page).
    # One model id per line (or comma-separated). Matched against Google's live list.
    extra_models  = models.TextField(_("Extra models to include"), blank=True, default='',
        help_text=_("Force these model ids into the dropdown even if auto-discovery would skip them "
                    "(must still exist in Google's live list). One per line."))
    hidden_models = models.TextField(_("Models to hide"), blank=True, default='',
        help_text=_("Hide these model ids from the dropdown. Substring match. One per line."))

    class Meta:
        verbose_name = _("AI Settings")
        verbose_name_plural = _("AI Settings")

    def save(self, *args, **kwargs):
        self.id = 1
        super().save(*args, **kwargs)

    @staticmethod
    def _parse_list(raw):
        """Split a textarea blob (newlines and/or commas) into clean lower-cased ids."""
        if not raw:
            return []
        parts = raw.replace(',', '\n').splitlines()
        return [p.strip().lower() for p in parts if p.strip()]

    @property
    def extra_model_list(self):
        return self._parse_list(self.extra_models)

    @property
    def hidden_model_list(self):
        return self._parse_list(self.hidden_models)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "AI Settings"

    @property
    def has_key(self):
        return bool(self.gemini_api_key)


# ---------- model catalog (refreshed from Gemini API) ----------

class AIModelCache(TimestampedModel):
    """Mirror of available Gemini models with their per-key quotas.

    Populated by `GeminiService.refresh_models()` — manual button (C4) and
    24h background job. AIProfile.model_id references rows here by `model_id`.
    """

    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    model_id  = models.CharField(max_length=120, unique=True, db_index=True,
                                 help_text=_("Gemini model name, e.g. 'gemini-2.5-flash'."))
    display_name = models.CharField(max_length=200, blank=True, default='')
    description  = models.TextField(blank=True, default='')

    # Per-key quotas as advertised by the API. NULL = unknown / not applicable.
    rpm    = models.PositiveIntegerField(null=True, blank=True, help_text=_("Requests per minute."))
    rpd    = models.PositiveIntegerField(null=True, blank=True, help_text=_("Requests per day."))
    tokens = models.PositiveIntegerField(null=True, blank=True, help_text=_("Token / TPM limit."))

    # Capability flags — drive the C4 "Not supported" labels.
    supports_thinking  = models.BooleanField(default=False)
    supports_grounding = models.BooleanField(default=False)
    supports_vision    = models.BooleanField(default=False)
    supports_audio     = models.BooleanField(default=False)

    last_refreshed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['model_id']
        verbose_name = _("AI Model (cached)")
        verbose_name_plural = _("AI Model Cache")

    def __str__(self):
        return self.display_name or self.model_id


# ---------- profiles ----------

class AIProfile(TimestampedModel, SoftDeleteModel):
    """A named AI persona. Exactly one is active at a time (`is_active=True`).

    `enabled_tools` is the per-profile allowlist into the global Tool Registry
    populated by C3 — stored as a list of tool names so missing tools degrade
    gracefully instead of breaking the profile.
    """

    class VisionResolution(models.TextChoices):
        AUTO = 'AUTO', _('Auto')
        LOW  = 'LOW',  _('Low')
        HIGH = 'HIGH', _('High')

    class ThinkingLevel(models.TextChoices):
        OFF    = 'OFF',    _('Off')
        LOW    = 'LOW',    _('Low')
        MEDIUM = 'MEDIUM', _('Medium')
        HIGH   = 'HIGH',   _('High')

    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name      = models.CharField(max_length=120)
    avatar    = models.ImageField(upload_to='ai_profile_avatars/', blank=True, null=True)
    is_active = models.BooleanField(default=False, db_index=True,
                                    help_text=_("Only one profile can be active at a time."))

    # Knowledge scope
    global_knowledge = models.BooleanField(default=True,
        help_text=_("If True, RAG queries the entire knowledge base. "
                    "If False, only chunks tagged with this profile's industries."))

    # Model + sampling
    model_id          = models.CharField(max_length=120, blank=True, default='',
                                         help_text=_("Matches AIModelCache.model_id. Blank = no model selected yet."))
    vision_resolution = models.CharField(max_length=10, choices=VisionResolution.choices,
                                         default=VisionResolution.AUTO)
    max_output_tokens = models.PositiveIntegerField(null=True, blank=True,
                                                    help_text=_("Blank = use model default."))
    thinking_level    = models.CharField(max_length=10, choices=ThinkingLevel.choices,
                                         default=ThinkingLevel.OFF, blank=True)
    top_p       = models.FloatField(null=True, blank=True, help_text=_("0.0 - 1.0. Blank = model default."))
    top_k       = models.PositiveIntegerField(null=True, blank=True, help_text=_("Blank = model default."))
    temperature = models.FloatField(null=True, blank=True, help_text=_("0.0 - 2.0. Blank = model default."))
    google_grounding = models.BooleanField(default=False,
        help_text=_("Enable Google Search grounding (when model supports it)."))

    # Behavior
    system_instruction = models.TextField(blank=True, default='',
        help_text=_("The persona prompt prepended to every conversation."))
    enabled_tools = models.JSONField(default=list, blank=True,
        help_text=_("List of tool names enabled for this profile. Empty = all registered tools."))

    class Meta:
        verbose_name = _("AI Profile")
        verbose_name_plural = _("AI Profiles")
        ordering = ['-is_active', 'name']

    def __str__(self):
        return f"{self.name}{' (active)' if self.is_active else ''}"

    def save(self, *args, **kwargs):
        # Enforce single-active-profile invariant in code (no partial unique
        # index because soft-deleted rows can sit at is_active=False harmlessly).
        if self.is_active:
            AIProfile.objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)


# ---------- conversations + messages ----------

class AIConversation(TimestampedModel, SoftDeleteModel):
    """A chat session for one sudo user, optionally pinned to an acting store.

    `acting_store` records which tenant the sudo user was impersonating when
    they started — pure metadata; auto-scoping at run time uses the live
    X-Store-ID header, not this field.
    """

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user         = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                     related_name='ai_conversations')
    acting_store = models.ForeignKey(Store, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='ai_conversations')
    profile      = models.ForeignKey(AIProfile, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='conversations')
    title        = models.CharField(max_length=200, blank=True, default='',
                                    help_text=_("Auto-set to first user message if blank."))

    class Meta:
        ordering = ['-updated_at']
        verbose_name = _("AI Conversation")
        verbose_name_plural = _("AI Conversations")
        indexes = [
            models.Index(fields=['user', '-updated_at']),
        ]

    def __str__(self):
        return self.title or f"Conversation {self.id}"


class AIMessage(TimestampedModel):
    """One turn in a conversation. `attachments` holds image/audio metadata
    (paths or inline-data refs) — content itself stays in the storage backend.
    """

    class Role(models.TextChoices):
        USER      = 'USER',      _('User')
        MODEL     = 'MODEL',     _('Model')
        SYSTEM    = 'SYSTEM',    _('System')
        TOOL      = 'TOOL',      _('Tool')

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(AIConversation, on_delete=models.CASCADE, related_name='messages')
    role         = models.CharField(max_length=10, choices=Role.choices, db_index=True)
    content      = models.TextField(blank=True, default='')
    attachments  = models.JSONField(default=list, blank=True,
        help_text=_("List of {kind, path, mime_type} entries for images / audio."))
    tool_calls   = models.JSONField(default=list, blank=True,
        help_text=_("Function calls the model issued on this turn (name + args + result)."))
    usage        = models.JSONField(default=dict, blank=True,
        help_text=_("Token usage from the API: {input_tokens, output_tokens, ...}"))

    class Meta:
        ordering = ['created_at']
        verbose_name = _("AI Message")
        verbose_name_plural = _("AI Messages")
        indexes = [
            models.Index(fields=['conversation', 'created_at']),
        ]

    def __str__(self):
        return f"{self.role}: {(self.content or '')[:60]}"


# ---------- knowledge base ----------

EMBEDDING_DIM = 768  # Gemini text-embedding-004 native dim


class AIKnowledgeChunk(TimestampedModel, SoftDeleteModel):
    """A retrievable chunk of admin knowledge. Indexed by HNSW over the
    embedding vector for fast cosine search.

    `industries` is a list of tags (e.g. ["retail", "fashion"]); a profile
    with `global_knowledge=False` only sees chunks tagged with one of its
    industries.
    """

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_name  = models.CharField(max_length=255,
        help_text=_("Original document filename or URL."))
    source_type  = models.CharField(max_length=20, default='document',
        help_text=_("Loose tag: 'document', 'manual', 'url', etc."))
    chunk_index  = models.PositiveIntegerField(default=0,
        help_text=_("Position of this chunk inside its source document."))
    content      = models.TextField()
    industries   = models.JSONField(default=list, blank=True,
        help_text=_("List of industry tags for filtering. Empty = no industry filter."))
    metadata     = models.JSONField(default=dict, blank=True)

    embedding    = VectorField(dimensions=EMBEDDING_DIM, null=True, blank=True)

    class Meta:
        verbose_name = _("AI Knowledge Chunk")
        verbose_name_plural = _("AI Knowledge Chunks")
        indexes = [
            HnswIndex(
                name='ai_kb_embedding_hnsw',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
        ]

    def __str__(self):
        return f"{self.source_name}#{self.chunk_index}"
