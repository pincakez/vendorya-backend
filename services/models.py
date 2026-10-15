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

    diagnosis     = models.TextField(blank=True)

    # ── Service desk (PROLINE-PARITY Upgrade 13, Proline tb_Ser_Service) ──
    # Who works on the job — a user of the SAME shop (checked in the serializer).
    technician    = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='service_jobs',
    )
    device        = models.CharField(max_length=200, blank=True)          # e.g. "Dell Latitude 5420"
    # Serial / IMEI / service tag as typed. Plain text on purpose: per-unit serials in inventory are a separate
    # feature and are linked later.
    device_serial = models.CharField(max_length=100, blank=True, db_index=True)
    damage        = models.TextField(blank=True)                          # the problem / damage the customer reports
    # Price split (Proline "detect value" vs "repair value"): `detection_fee` = the check-up / diagnosis charge,
    # `cost` (kept, unchanged meaning) = the repair charge. The job's labour line = detection_fee + cost.
    detection_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    # Repair under warranty = no charge: Done bills the labour and the parts at 0 (parts still leave stock).
    is_warranty   = models.BooleanField(default=False)
    # Warranty the shop gives on THIS repair (0 = none) — printed on the service ticket.
    warranty_days = models.PositiveIntegerField(default=0)

    notify_bell   = models.BooleanField(default=False)
    notified      = models.BooleanField(default=False)

    # Linked invoice — set when service is marked Done
    invoice       = models.ForeignKey(
        'finance.SalesInvoice', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='service',
    )

    # The return that gave the money back when a paid job was marked Returned (finance RefundInvoice).
    refund        = models.ForeignKey(
        'finance.RefundInvoice', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='service_returns',
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
            models.Index(fields=['store', 'technician']),
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

    @property
    def labour_total(self):
        """What the job charges for the work itself (before tax): detection fee + repair charge."""
        return (self.detection_fee or Decimal('0')) + (self.cost or Decimal('0'))


class ServicePart(TimestampedModel, SoftDeleteModel):
    """A part taken from stock for a repair (Proline tb_Ser_Item).

    Planning only while the job is open — NOTHING moves stock here. When the job is marked Done each live part
    becomes a real line on the job's sales invoice, and the stock leaves through the normal sale checkout
    (oversell check, FEFO batches, COGS snapshot, serial units → SOLD). A returned job gives the parts back
    through a normal return (serial units back on the shelf).
    """
    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store      = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='service_parts')
    service    = models.ForeignKey(Service, on_delete=models.CASCADE, related_name='parts')
    variant    = models.ForeignKey('inventory.ProductVariant', on_delete=models.PROTECT, related_name='service_parts')
    quantity   = models.DecimalField(max_digits=10, decimal_places=3)       # in the variant's base unit
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)       # selling price per unit, before tax
    # A serial-tracked product: the exact unit(s) used, one serial per unit (checked IN_STOCK while the job is
    # open). Done copies them onto the sale line, so the normal posting marks them SOLD with warranty.
    serial_numbers = models.JSONField(default=list, blank=True)

    objects     = TenantSoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ['created_at']

    @property
    def line_total(self):
        return (self.quantity or Decimal('0')) * (self.unit_price or Decimal('0'))


class ServiceNote(TimestampedModel, SoftDeleteModel):
    """One dated note on a job, by a user (Proline tb_Ser_Service_Tasks) — the job's timeline."""
    id      = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store   = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='service_notes')
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name='notes')
    user    = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name='service_notes')
    body    = models.TextField()

    objects     = TenantSoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ['created_at']


class ServiceDeskSettings(models.Model):
    """Per-shop service-desk wiring. `labour_variant` = the shop's ONE system "Repair service" product
    (type SERVICE = no stock) that a job's labour line points at on its sales invoice. Made on first use by
    services.labour.labour_variant() — like finance.split_payment.store_credit_method()."""
    store          = models.OneToOneField(Store, on_delete=models.CASCADE, related_name='service_desk')
    labour_variant = models.ForeignKey('inventory.ProductVariant', null=True, blank=True,
                                       on_delete=models.SET_NULL, related_name='+')
