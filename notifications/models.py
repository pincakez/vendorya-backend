import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import Store


class Notification(models.Model):
    """Per-user inbox entry — the bell icon's source of truth.

    `user` nullable means store-wide (visible to anyone in the store).
    """

    class Type(models.TextChoices):
        BILLING_INVOICE   = 'BILLING_INVOICE',   _('Billing Invoice')
        BILLING_PAID      = 'BILLING_PAID',      _('Payment Received')
        SUBSCRIPTION      = 'SUBSCRIPTION',      _('Subscription')
        LOW_STOCK         = 'LOW_STOCK',         _('Low Stock')
        SHIFT_DIFFERENCE  = 'SHIFT_DIFFERENCE',  _('Shift Difference')
        SYSTEM            = 'SYSTEM',            _('System')
        GENERAL           = 'GENERAL',           _('General')

    id    = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='notifications')
    user  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              null=True, blank=True, related_name='notifications')

    type    = models.CharField(max_length=30, choices=Type.choices, default=Type.GENERAL, db_index=True)
    title   = models.CharField(max_length=200)
    body    = models.TextField(blank=True)
    link    = models.CharField(max_length=500, blank=True,
                               help_text=_("Frontend route the bell click should open."))
    payload = models.JSONField(default=dict, blank=True)

    read_at    = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes  = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['store', '-created_at']),
            models.Index(fields=['user', 'read_at']),
        ]

    def __str__(self):
        return f"{self.type}: {self.title}"

    @property
    def is_unread(self):
        return self.read_at is None
