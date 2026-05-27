import uuid
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from core.models import TimestampedModel, Store

class TablePreference(TimestampedModel):
    """Stores user preferences for specific data tables (columns, sorting, etc)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='table_preferences')
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='table_preferences')
    
    table_id = models.CharField(_("Table ID"), max_length=100, help_text="Unique ID used by frontend (e.g., 'inventory_list')")
    
    # We store the configuration as JSON. 
    # This is flexible: { "columns": ["name", "price"], "sort": "price", "filters": {...} }
    config = models.JSONField(_("Configuration"), default=dict)

    class Meta:
        unique_together = ('user', 'table_id')
        verbose_name = _("Table Preference")
        verbose_name_plural = _("Table Preferences")

    def __str__(self):
        return f"{self.user.username} - {self.table_id}"