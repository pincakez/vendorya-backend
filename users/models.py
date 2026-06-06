import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _
from core.models import Address, TimestampedModel, SoftDeleteModel, Store
from core.tenancy import TenantSoftDeleteManager
class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER = 'OWNER', _('Owner')
        ADMIN = 'ADMIN', _('Admin')
        MANAGER = 'MANAGER', _('Manager')
        CASHIER = 'CASHIER', _('Cashier')

    store = models.ForeignKey(Store, related_name='staff', on_delete=models.CASCADE, null=True, blank=True)
    role = models.CharField(_("Role"), max_length=50, choices=Role.choices, default=Role.CASHIER)
    photo = models.ImageField(_("User Photo"), upload_to='user_photos/', blank=True, null=True)
    is_superadmin = models.BooleanField(
        _("Super Admin"),
        default=False,
        help_text=_("Vendorya platform-level admin. Bypasses per-store filtering via X-Store-ID header."),
    )
    force_password_change = models.BooleanField(
        _("Force Password Change"),
        default=False,
        help_text=_("Set when an admin issues a temp password. User must change it on next login."),
    )
    phone_number = models.CharField(_("Phone Number"), max_length=20, blank=True, default='')
    whatsapp_number = models.CharField(_("WhatsApp Number"), max_length=20, blank=True, default='')

    def __str__(self):
        return self.username

class Customer(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='customers')
    name = models.CharField(_("Full Name"), max_length=255)
    phone_number = models.CharField(_("Phone Number"), max_length=20, help_text=_("Phone number must be unique per store."))
    notes = models.TextField(_("Notes"), blank=True, null=True)
    shipping_address = models.ForeignKey(Address, related_name='shipping_customers', on_delete=models.SET_NULL, null=True, blank=True)
    billing_address = models.ForeignKey(Address, related_name='billing_customers', on_delete=models.SET_NULL, null=True, blank=True)
    
    # NEW: Track Debt
    balance = models.DecimalField(_("Current Balance"), max_digits=12, decimal_places=2, default=0.00, help_text="Positive = They owe us. Negative = We owe them.")
    credit_limit = models.DecimalField(
        _("Credit Limit"), max_digits=12, decimal_places=2,
        null=True, blank=True,
        help_text=_("Per-customer override. Null = use store default."),
    )

    # The store's default "Walk-in" customer for anonymous POS sales. One per store,
    # auto-created on store creation. Not user-editable/deletable; POS auto-selects it.
    is_walk_in = models.BooleanField(default=False, editable=False)

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

    class Meta:
        verbose_name = _("Customer")
        verbose_name_plural = _("Customers")
        unique_together = ('store', 'phone_number')
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.phone_number})"