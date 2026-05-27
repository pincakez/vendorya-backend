import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import Store, TimestampedModel, SoftDeleteModel


class SubscriptionPlan(TimestampedModel, SoftDeleteModel):
    """A pricing tier sudo can define. Free-form name — no enum."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(_("Plan Name"), max_length=80, unique=True,
                            help_text=_("Free-form. e.g. 'GO', 'Pro', 'Beta Friends'."))
    description = models.TextField(blank=True)

    monthly_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    annual_price  = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    currency      = models.CharField(max_length=10, default='EGP')

    # Quotas. NULL means "unlimited / not enforced today."
    max_users     = models.PositiveIntegerField(null=True, blank=True)
    max_branches  = models.PositiveIntegerField(null=True, blank=True)
    max_products  = models.PositiveIntegerField(null=True, blank=True)
    max_invoices_per_month = models.PositiveIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True,
                                    help_text=_("Inactive plans can't be assigned to new subscriptions."))

    class Meta:
        ordering = ['monthly_price', 'name']

    def __str__(self):
        return self.name


class Subscription(TimestampedModel):
    """One per Store. Owns the active plan + billing-cycle metadata."""

    class Status(models.TextChoices):
        TRIAL     = 'TRIAL',     _('Trial')
        ACTIVE    = 'ACTIVE',    _('Active')
        PAST_DUE  = 'PAST_DUE',  _('Past Due')
        CANCELLED = 'CANCELLED', _('Cancelled')

    id    = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name='subscription')
    plan  = models.ForeignKey(SubscriptionPlan, on_delete=models.PROTECT, related_name='subscriptions')

    # Free-form override. When set, displayed instead of plan.name in tenant + admin UI.
    custom_label = models.CharField(max_length=120, blank=True,
                                    help_text=_("Optional per-tenant label, e.g. 'Hossam Special — free forever'."))

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    period_start  = models.DateField(null=True, blank=True)
    period_end    = models.DateField(null=True, blank=True)
    trial_ends_at = models.DateField(null=True, blank=True)
    cancelled_at  = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True, help_text=_("Internal sudo notes — never shown to tenant."))

    class Meta:
        ordering = ['store__name']

    def __str__(self):
        return f"{self.store.name} — {self.display_label}"

    @property
    def display_label(self):
        return self.custom_label.strip() or self.plan.name


class BillingInvoice(TimestampedModel):
    """Invoice Vendorya issues to a tenant store.

    Distinct from `finance.SalesInvoice` (which is the tenant's own customer
    invoice).  Numbering is global across the platform (`INV-YYYY-NNNNNN`).
    """

    class Status(models.TextChoices):
        DRAFT  = 'DRAFT',  _('Draft')
        ISSUED = 'ISSUED', _('Issued')
        PAID   = 'PAID',   _('Paid')
        VOID   = 'VOID',   _('Void')

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name='invoices')
    store        = models.ForeignKey(Store, on_delete=models.PROTECT, related_name='billing_invoices')

    invoice_number = models.CharField(max_length=40, unique=True, blank=True,
                                      help_text=_("Auto-generated on issue: INV-YYYY-NNNNNN"))
    status         = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    amount   = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=10, default='EGP')

    period_start = models.DateField(null=True, blank=True)
    period_end   = models.DateField(null=True, blank=True)

    issued_at      = models.DateTimeField(null=True, blank=True)
    due_at         = models.DateField(null=True, blank=True)
    paid_at        = models.DateTimeField(null=True, blank=True)
    paid_method    = models.CharField(max_length=40, blank=True)
    paid_reference = models.CharField(max_length=120, blank=True)

    line_description = models.CharField(max_length=255, blank=True,
                                        help_text=_("Free-text shown on the printable invoice."))
    notes = models.TextField(blank=True)

    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at']
        indexes  = [models.Index(fields=['store', '-created_at'])]

    def __str__(self):
        return f"{self.invoice_number or 'DRAFT'} — {self.store.name}"

    def _next_number(self):
        year   = timezone.now().year
        prefix = f"INV-{year}-"
        with transaction.atomic():
            last = (BillingInvoice.objects
                    .select_for_update()
                    .filter(invoice_number__startswith=prefix)
                    .order_by('-invoice_number')
                    .first())
            if last and last.invoice_number[-6:].isdigit():
                seq = int(last.invoice_number[-6:]) + 1
            else:
                seq = 1
            return f"{prefix}{seq:06d}"

    def issue(self, by_user=None):
        """DRAFT → ISSUED. Assigns invoice_number, stamps issued_at, signals an inbox notification."""
        if self.status != self.Status.DRAFT:
            return self
        self.invoice_number = self._next_number()
        self.status         = self.Status.ISSUED
        self.issued_at      = timezone.now()
        if by_user:
            self.issued_by = by_user
        self.save()
        return self

    def mark_paid(self, method='', reference=''):
        if self.status != self.Status.ISSUED:
            return self
        self.status         = self.Status.PAID
        self.paid_at        = timezone.now()
        self.paid_method    = method or self.paid_method
        self.paid_reference = reference or self.paid_reference
        self.save()
        return self
