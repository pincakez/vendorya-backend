import uuid
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from core.models import TimestampedModel, Store


class TablePreset(TimestampedModel):
    """Layer 2 — a named column layout for a table, owned by the store.

    Authored by OWNER/ADMIN/sudo. `config` shape:
      { "order": [keys...], "hidden": [keys...],
        "widths": {key: px}, "sort": {"key":..,"dir":"asc|desc"}, "page_size": 50 }
    Presets are cosmetic only — they can never reveal a Layer-1-hidden field.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='table_presets')
    table_id = models.CharField(_("Table ID"), max_length=100)
    name = models.CharField(_("Preset Name"), max_length=100)
    config = models.JSONField(_("Configuration"), default=dict)
    is_default = models.BooleanField(_("Store Default"), default=False,
                                     help_text=_("Used for users without an explicit assignment."))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')

    class Meta:
        unique_together = ('store', 'table_id', 'name')
        verbose_name = _("Table Preset")
        verbose_name_plural = _("Table Presets")

    def __str__(self):
        return f"{self.store_id} · {self.table_id} · {self.name}"


class TablePreference(TimestampedModel):
    """Per-user, per-table assignment: which preset this user loads by default.

    Ad-hoc tweaks (quick resize/sort outside edit mode) live in the browser
    (localStorage), not here — this row only records the assigned preset.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='table_preferences')
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='table_preferences')
    table_id = models.CharField(_("Table ID"), max_length=100)
    assigned_preset = models.ForeignKey(TablePreset, on_delete=models.SET_NULL,
                                        null=True, blank=True, related_name='assignments')
    config = models.JSONField(_("Configuration"), default=dict, blank=True)

    class Meta:
        unique_together = ('user', 'table_id')
        verbose_name = _("Table Preference")
        verbose_name_plural = _("Table Preferences")

    def __str__(self):
        return f"{self.user.username} - {self.table_id}"
