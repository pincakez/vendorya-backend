import uuid
from django.db import models
from django.conf import settings
from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

# --- MANAGERS ---
class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

class GlobalManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset()

# --- ABSTRACT MODELS ---
class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ['-updated_at', '-created_at']

class SoftDeleteModel(models.Model):
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = GlobalManager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save()

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.save()

# --- CONCRETE MODELS ---
class Currency(TimestampedModel, SoftDeleteModel):
    """Display currency for a tenant store. Sudo-managed master list."""

    class Position(models.TextChoices):
        PREFIX = 'PREFIX', _('Prefix (e.g. $100)')
        SUFFIX = 'SUFFIX', _('Suffix (e.g. 100 LE)')

    id     = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code   = models.CharField(max_length=10, unique=True,
                              help_text=_("Short identifier. ISO if possible (EGP, USD, EUR) but free-form."))
    symbol = models.CharField(max_length=10,
                              help_text=_("How it renders to the user. e.g. 'EGP', 'LE', '$', '€'."))
    name   = models.CharField(max_length=80)
    position  = models.CharField(max_length=10, choices=Position.choices, default=Position.SUFFIX)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("Currency")
        verbose_name_plural = _("Currencies")
        ordering = ['code']

    def __str__(self):
        return f"{self.symbol} ({self.code})"


class Store(TimestampedModel, SoftDeleteModel):
    class SubscriptionPlan(models.TextChoices):
        FREE = 'FREE', _('Free')
        PREMIUM = 'PREMIUM', _('Premium')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(_("Store Name"), max_length=200)
    store_code = models.CharField(
        _("Store Code"),
        max_length=3,
        unique=True,
        null=True,
        blank=True,
        validators=[RegexValidator(r'^\d{3}$', _('Store code must be exactly 3 digits (000–999).'))],
        help_text=_("Globally unique 3-digit code — forms the first segment of every SKU in this store.")
    )
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='owned_stores', on_delete=models.CASCADE)
    plan = models.CharField(max_length=20, choices=SubscriptionPlan.choices, default=SubscriptionPlan.FREE)
    is_active = models.BooleanField(default=True)

    # Defaults
    default_supplier = models.ForeignKey('inventory.Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    default_category = models.ForeignKey('inventory.Category', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    default_language = models.CharField(max_length=5, default='ar')

    # Contact
    phone_number    = models.CharField(_("Phone Number"),    max_length=20, blank=True, default='')
    whatsapp_number = models.CharField(_("WhatsApp Number"), max_length=20, blank=True, default='')
    city    = models.CharField(_("City"),    max_length=100, blank=True, default='')
    country = models.CharField(_("Country"), max_length=100, blank=True, default='Egypt')

    # Localization. Server clock is the source of truth — store just picks
    # which IANA zone to render in. Defaults to Cairo.
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name='stores',
                                 null=True, blank=True,
                                 help_text=_("Currency used everywhere this store displays money."))
    timezone = models.CharField(max_length=64, default='Africa/Cairo',
                                help_text=_("IANA timezone for rendering dates/times to the store."))

    class Meta:
        verbose_name = _("Store")
        verbose_name_plural = _("Stores")

    def __str__(self):
        return self.name

class Address(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='addresses')
    street_1 = models.CharField(_("Street Address 1"), max_length=255)
    street_2 = models.CharField(_("Street Address 2"), max_length=255, blank=True, null=True)
    city = models.CharField(_("City"), max_length=100)
    country = models.CharField(_("Country"), max_length=100, default=_("Egypt"))

    def __str__(self):
        return f"{self.street_1}, {self.city}"

class Branch(TimestampedModel, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='branches')
    name = models.CharField(_("Branch Name"), max_length=150)
    address = models.OneToOneField(Address, on_delete=models.PROTECT, verbose_name=_("Address"))
    is_main_branch = models.BooleanField(default=False)
    phone_number = models.CharField(_("Phone Number"), max_length=20, blank=True, default='')
    email = models.EmailField(_("Email"), max_length=254, blank=True, default='')

    class Meta:
        verbose_name = _("Branch")
        verbose_name_plural = _("Branches")

    def __str__(self):
        return self.name

# --- AUDIT LOGS ---
class ActivityLog(models.Model):
    """Tracks user actions for security and auditing."""

    class OperationType(models.TextChoices):
        SALE       = 'SALE',       _('Sale')
        RETURN     = 'RETURN',     _('Return')
        DISCOUNT   = 'DISCOUNT',   _('Discount')
        PURCHASE   = 'PURCHASE',   _('Purchase')
        ADJUSTMENT = 'ADJUSTMENT', _('Stock Adjustment')
        EXPENSE    = 'EXPENSE',    _('Expense')
        SHIFT      = 'SHIFT',      _('Shift')
        STAFF      = 'STAFF',      _('Staff')
        OTHER      = 'OTHER',      _('Other')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='logs')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)

    operation_type = models.CharField(
        max_length=20, choices=OperationType.choices, default=OperationType.OTHER, db_index=True,
    )
    action = models.CharField(max_length=255)  # e.g., "Created Invoice #1001"
    details = models.JSONField(default=dict, blank=True)  # e.g., {"total": 500, "items": 3}

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['store', '-timestamp']),
            models.Index(fields=['store', 'operation_type', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.user} - {self.action}"
    
    # --- STORE SETTINGS ---
class StoreSettings(TimestampedModel):
    """Configuration for a specific store."""
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name='settings')

    # 1. Inventory Rules
    allow_negative_stock = models.BooleanField(default=False, help_text="If False, POS will block sales when stock is insufficient.")

    # 2. Sales Rules
    enable_agel_selling = models.BooleanField(default=True, help_text="Allow selling on credit (Customer Debt).")
    default_tax = models.ForeignKey('inventory.Tax', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    # 3. Number formatting (user-facing display rules)
    decimals = models.PositiveSmallIntegerField(default=2,
                                                help_text=_("Number of decimal places shown for prices/totals (0-4)."))
    thousands_separator = models.BooleanField(default=False,
                                              help_text=_("Show thousand separators in numbers (off by default)."))

    # 4. Legal & Receipt Info
    tax_id = models.CharField(_("Tax ID / Betaka"), max_length=50, blank=True)
    commercial_reg = models.CharField(_("Commercial Reg / Sogel"), max_length=50, blank=True)
    receipt_header = models.TextField(_("Receipt Header"), blank=True, help_text="Text to appear at the top of the receipt.")
    receipt_footer = models.TextField(_("Receipt Footer"), blank=True, help_text="Text to appear at the bottom (e.g., Return Policy).")

    # 5. SKU / Numbering
    class ProductNumberingMode(models.TextChoices):
        PROGRESSIVE = 'PROGRESSIVE', _('Progressive (0001, 0002 …)')
        RANDOM      = 'RANDOM',      _('Random (4-digit unique)')

    product_numbering_mode = models.CharField(
        _("Product Numbering Mode"),
        max_length=20,
        choices=ProductNumberingMode.choices,
        default=ProductNumberingMode.PROGRESSIVE,
    )

    # 6. Security (Auth Hardening)
    session_timeout_minutes = models.PositiveSmallIntegerField(
        _("Session Timeout (minutes)"), default=0,
        help_text=_("Auto-logout after this many minutes idle. 0 = disabled. Enforced client-side."))
    login_ip_allowlist = models.TextField(
        _("Login IP Allowlist"), blank=True,
        help_text=_("Restrict OWNER/ADMIN logins to these IPs/CIDRs (comma or newline separated). Empty = no restriction."))
    force_2fa_managers = models.BooleanField(
        _("Force 2FA for Managers+"), default=False,
        help_text=_("Require TOTP two-factor auth for all staff with role Manager or higher."))

    # 7. Field Visibility (Layer 1 — server-enforced, per-store, by role)
    field_visibility = models.JSONField(
        _("Field Visibility"), default=dict, blank=True,
        help_text=_("Per-role hidden columns: {table_id: {ROLE: [field, ...]}}. "
                    "Server omits these fields from API responses for that role. "
                    "Empty = use built-in defaults."))

    def __str__(self):
        return f"Settings for {self.store.name}"

# Signal to auto-create settings when a Store is created
from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=Store)
def create_store_settings(sender, instance, created, **kwargs):
    if created:
        StoreSettings.objects.create(store=instance)