import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import Store

SOUND_CHOICES = [('mute', 'Mute')] + [(f's{i:02d}', f'Sound {i:02d}') for i in range(1, 11)]


class Notification(models.Model):

    class Priority(models.TextChoices):
        INFO    = 'INFO',    _('Information')
        WARNING = 'WARNING', _('Warning')
        ALERT   = 'ALERT',   _('Alert')
        ADMIN   = 'ADMIN',   _('Admin Note')

    class Type(models.TextChoices):
        BILLING_INVOICE   = 'BILLING_INVOICE',   _('Billing Invoice')
        BILLING_PAID      = 'BILLING_PAID',       _('Payment Received')
        SUBSCRIPTION      = 'SUBSCRIPTION',       _('Subscription')
        LOW_STOCK         = 'LOW_STOCK',          _('Low Stock')
        SHIFT_DIFFERENCE  = 'SHIFT_DIFFERENCE',   _('Shift Difference')
        INVOICE_VOIDED    = 'INVOICE_VOIDED',     _('Invoice Voided')
        ADMIN_NOTE        = 'ADMIN_NOTE',         _('Admin Note')
        SYSTEM            = 'SYSTEM',             _('System')
        GENERAL           = 'GENERAL',            _('General')

    id    = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='notifications')
    user  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              null=True, blank=True, related_name='notifications')

    priority = models.CharField(max_length=10, choices=Priority.choices,
                                default=Priority.INFO, db_index=True)
    type    = models.CharField(max_length=30, choices=Type.choices,
                               default=Type.GENERAL, db_index=True)
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
            models.Index(fields=['store', 'priority', '-created_at']),
        ]

    def __str__(self):
        return f"{self.priority}: {self.title}"

    @property
    def is_unread(self):
        return self.read_at is None


class NotificationPreference(models.Model):
    """Per-user sound + visibility preferences for each notification priority."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='notification_prefs',
    )

    info_enabled    = models.BooleanField(default=True)
    warning_enabled = models.BooleanField(default=True)
    alert_enabled   = models.BooleanField(default=True)
    # ADMIN priority cannot be disabled — no field needed

    info_sound    = models.CharField(max_length=10, choices=SOUND_CHOICES, default='s01')
    warning_sound = models.CharField(max_length=10, choices=SOUND_CHOICES, default='s02')
    alert_sound   = models.CharField(max_length=10, choices=SOUND_CHOICES, default='s03')
    admin_sound   = models.CharField(max_length=10, choices=SOUND_CHOICES, default='s01')

    def __str__(self):
        return f"Prefs({self.user})"
