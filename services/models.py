import uuid
from decimal import Decimal
from django.db import models, transaction
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import TimestampedModel, SoftDeleteModel, Store, Branch
from core.tenancy import TenantSoftDeleteManager


class ServiceSequence(models.Model):
    """Per-store counter for human-readable service serial numbers."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('store',)


class Service(TimestampedModel, SoftDeleteModel):
    class Status(models.TextChoices):
        OPEN      = 'OPEN',      _('Open')
        DONE      = 'DONE',      _('Done')
        CANCELLED = 'CANCELLED', _('Cancelled')
        ARCHIVED  = 'ARCHIVED',  _('Archived')
        RETURNED  = 'RETURNED',  _('Returned')

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store         = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='services')
    serial_number = models.CharField(max_length=20, editable=False, db_index=True)

    # Client info — either a registered customer or free-text
    client        = models.ForeignKey(
        'users.Customer', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='services',
    )
    client_name   = models.CharField(max_length=200, blank=True)
    client_phone  = models.CharField(max_length=30, blank=True)

    service_type  = models.CharField(max_length=100, blank=True)
    receive_date  = models.DateField()

    # ETA — no_eta=True means "No ETA" checkbox is checked, dropdowns greyed out
    no_eta        = models.BooleanField(default=True)
    eta_days      = models.PositiveSmallIntegerField(null=True, blank=True)
    eta_hours     = models.PositiveSmallIntegerField(null=True, blank=True)
    eta_datetime  = models.DateTimeField(null=True, blank=True, db_index=True)

    info          = models.TextField(blank=True)
    keeping       = models.CharField(max_length=500, blank=True)
    cost          = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    status        = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True,
    )

    notify_bell   = models.BooleanField(default=False)
    notified      = models.BooleanField(default=False)

    # Linked invoice — set when service is marked Done
    invoice       = models.ForeignKey(
        'finance.SalesInvoice', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='service',
    )

    created_by    = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='created_services',
    )

    objects     = TenantSoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['store', 'status']),
            models.Index(fields=['store', 'eta_datetime']),
        ]

    def __str__(self):
        return f"{self.serial_number} — {self.service_type or 'Service'}"

    def save(self, *args, **kwargs):
        if not self.serial_number:
            with transaction.atomic():
                seq, _ = ServiceSequence.objects.select_for_update().get_or_create(store=self.store)
                seq.last_number += 1
                seq.save()
                self.serial_number = f"SRV-{seq.last_number:03d}"

        # Compute eta_datetime from receive_date + eta_days/hours
        if not self.no_eta and self.receive_date is not None:
            from datetime import datetime, timedelta
            base = datetime.combine(self.receive_date, datetime.min.time())
            if timezone.is_naive(base):
                base = timezone.make_aware(base)
            self.eta_datetime = base + timedelta(
                days=self.eta_days or 0,
                hours=self.eta_hours or 0,
            )
        else:
            self.eta_datetime = None

        super().save(*args, **kwargs)
