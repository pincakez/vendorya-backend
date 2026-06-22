import uuid
from django.db import models
from django.conf import settings
from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from core.tenancy import TenantSoftDeleteManager, TenantScopedManager

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

    class StoreType(models.TextChoices):
        # The store's retail vertical. Cosmetic on its own — its only behavioural
        # job is to seed sensible *defaults* for the opt-in capability switches at
        # creation time (see AdminStoreCreateSerializer). It NEVER locks anything;
        # the owner can flip every switch afterwards in Settings → Capabilities.
        GENERAL     = 'GENERAL',     _('General Retail')
        PHARMACY    = 'PHARMACY',    _('Pharmacy')
        GROCERY     = 'GROCERY',     _('Grocery / Supermarket')
        ELECTRONICS = 'ELECTRONICS', _('Electronics / Devices')
        CLOTHING    = 'CLOTHING',    _('Clothing / Fashion')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(_("Store Name"), max_length=200)
    store_type = models.CharField(
        _("Store Type"), max_length=20,
        choices=StoreType.choices, default=StoreType.GENERAL,
        help_text=_("Retail vertical. Seeds default capability switches at creation; "
                    "never locks anything — owner can change every switch later."))
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
    address_line = models.CharField(_("Address"), max_length=255, blank=True, default='')
    email   = models.EmailField(_("Email Address"), blank=True, default='')
    website = models.URLField(_("Website"), max_length=200, blank=True, default='')
    fb_page = models.CharField(_("Facebook Page"), max_length=200, blank=True, default='')
    instagram = models.CharField(_("Instagram"), max_length=200, blank=True, default='')

    # Branding logos (480×112px recommended). Two variants so the app can
    # switch automatically when the user toggles dark/light mode.
    logo_light = models.ImageField(upload_to='store_logos/', null=True, blank=True,
                                   help_text=_("Store logo for light mode (recommended 480×112 px, PNG/SVG)."))
    logo_dark  = models.ImageField(upload_to='store_logos/', null=True, blank=True,
                                   help_text=_("Store logo for dark mode (recommended 480×112 px, PNG/SVG)."))

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

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

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

    objects = TenantSoftDeleteManager()   # secure-by-default; .all_objects = unscoped

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

    objects     = TenantScopedManager()   # secure-by-default: auto-scopes to the active tenant
    all_objects = models.Manager()         # escape hatch (sudo audit, analytics, purge command)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['store', '-timestamp']),
            models.Index(fields=['store', 'operation_type', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.user} - {self.action}"
    
def _default_category_levels():
    return ['Category', 'Sub-category', 'Sub-category 2', 'Sub-category 3']


def _default_unit_tiers():
    # Store-wide default names for the two units above the base (e.g. a pharmacy's
    # Strip then Pack). Used to seed the per-product unit ladder in the New Product
    # modal; each product can still override. Index 0 = first tier above base.
    return ['Strip', 'Pack']


    # --- STORE SETTINGS ---
class StoreSettings(TimestampedModel):
    """Configuration for a specific store."""
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name='settings')

    # 1. Inventory Rules
    allow_negative_stock = models.BooleanField(default=False, help_text="If False, POS will block sales when stock is insufficient.")

    # 1b. Expiry / Batch (FEFO) tracking — opt-in master switch.
    # OFF by default: the whole feature is dormant and invisible, so non-pharmacy
    # stores (e.g. Gates) are completely unaffected. ON unlocks per-product
    # `track_expiry` (inventory.Product) + batch capture on purchases + FEFO
    # draw-down on sales. See inventory.StockBatch / finance FEFO engine.
    expiry_tracking_enabled = models.BooleanField(
        _("Expiry / Batch Tracking"), default=False,
        help_text=_("Master switch for pharmacy-grade expiry & batch (FEFO) tracking. "
                    "Off = feature hidden; existing stores behave exactly as before."))

    # 1c. Multi-unit (UoM) master switch — opt-in selling in packs/strips on top of
    # the base unit. Defaults ON: the s97 engine has shipped always-on per product,
    # so existing multi-unit products MUST keep working. Off hides alternate units
    # at POS / on products (rows are preserved in DB, just not offered) → a product
    # behaves as a single base unit. See inventory.is_multi_unit_enabled.
    multi_unit_enabled = models.BooleanField(
        _("Multi-Unit Selling"), default=True,
        help_text=_("Master switch for selling one product in multiple units "
                    "(base + packs/strips). Off = only the base unit is offered; "
                    "existing alternate units are hidden, not deleted."))

    # 1d. Weight-based selling master switch (Phase C). Off by default — the
    # per-product weight mode + POS decimal entry are gated on this.
    weight_selling_enabled = models.BooleanField(
        _("Weight-Based Selling"), default=False,
        help_text=_("Master switch for selling products by weight (per kg / 100g, "
                    "decimal quantities). Off = feature hidden."))

    class ExpiredSalePolicy(models.TextChoices):
        ALLOW = 'ALLOW', _('Allow — sell expired stock silently')
        WARN  = 'WARN',  _('Warn — flag expired stock at POS but allow')
        BLOCK = 'BLOCK', _('Block — reject a sale that would draw expired stock')

    expired_sale_policy = models.CharField(
        _("Expired-Stock Sale Policy"), max_length=5,
        choices=ExpiredSalePolicy.choices, default=ExpiredSalePolicy.WARN,
        help_text=_("What happens when a FEFO draw would pull from an expired batch."))
    expiry_alert_days = models.PositiveIntegerField(
        _("Expiry Alert Window (days)"), default=60,
        help_text=_("Batches expiring within this many days are flagged 'expiring soon'."))

    # 2. Sales Rules
    enable_agel_selling = models.BooleanField(default=True, help_text="Allow selling on credit (Customer Debt).")

    class CreditPolicy(models.TextChoices):
        ALLOW = 'ALLOW', _('Allow — no enforcement')
        WARN  = 'WARN',  _('Warn — allow but notify store owner')
        BLOCK = 'BLOCK', _('Block — reject the sale')

    credit_policy = models.CharField(
        _("Credit Policy"), max_length=5,
        choices=CreditPolicy.choices, default=CreditPolicy.ALLOW,
    )
    default_credit_limit = models.DecimalField(
        _("Default Credit Limit"), max_digits=12, decimal_places=2,
        null=True, blank=True,
        help_text=_("Max unpaid balance a customer may carry. Null = no limit."),
    )
    default_tax = models.ForeignKey('inventory.Tax', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    tax_enabled = models.BooleanField(
        default=True,
        help_text=_("Master switch. When off, sales charge no tax and POS hides the "
                    "tax row — regardless of any product/default tax."))

    # 2b. Returns policy
    return_window_days = models.PositiveIntegerField(
        _("Return Window (days)"), default=0,
        help_text=_("Reject returns whose original invoice is older than this many "
                    "days. 0 = no limit."))
    restocking_fee_percent = models.DecimalField(
        _("Restocking Fee (%)"), max_digits=5, decimal_places=2, default=0,
        help_text=_("Default percentage deducted from a refund payout. 0 = none."))

    # 3. Number formatting (user-facing display rules)
    decimals = models.PositiveSmallIntegerField(default=2,
                                                help_text=_("Number of decimal places shown for prices/totals (0-4)."))
    thousands_separator = models.BooleanField(default=False,
                                              help_text=_("Show thousand separators in numbers (off by default)."))

    # Terminology — what this store calls a catalog item (label only).
    class ItemNoun(models.TextChoices):
        NAME    = 'NAME',    _('Name')
        PRODUCT = 'PRODUCT', _('Product')
        ITEM    = 'ITEM',    _('Item')
        MODEL   = 'MODEL',   _('Model')

    item_noun = models.CharField(
        _("Items are called"), max_length=10,
        choices=ItemNoun.choices, default=ItemNoun.NAME,
        help_text=_("Word used for a catalog item across the UI (display only)."))

    # Store-wide default name for the smallest/base quantity unit (e.g. "Pill",
    # "Tablet", "pcs"). Seeds Product.unit for new products; per-product override
    # still applies. Set in Settings → Business Rules, beside "Items are called".
    base_unit_name = models.CharField(
        _("Quantity is called"), max_length=20, default="pcs",
        help_text=_("Default name for a single base unit of stock (e.g. Pill, Tablet)."))

    # Store-wide default names for the two units above the base (Strip, Pack).
    # Edited in Capabilities when multi-unit selling is on; seeds the New Product
    # modal's tier rows. Each product can still rename/override its own units.
    unit_tier_names = models.JSONField(
        _("Unit tier names"), default=_default_unit_tiers,
        help_text=_("Default names for the units above the base (e.g. Strip, Pack)."))

    category_level_names = models.JSONField(
        _("Category level names"), default=_default_category_levels,
        help_text=_("Display names for the 4 category tiers, e.g. Type / Category / Spec. Label only."))

    # 4. Legal & Receipt Info
    tax_id = models.CharField(_("Tax ID / Betaka"), max_length=50, blank=True)
    print_tax_id = models.BooleanField(
        _("Print Tax ID on invoices"), default=True,
        help_text=_("When off, the Tax ID is omitted entirely from printed invoices "
                    "(no 'N/A' placeholder). Turn off if the store is not tax-registered."))
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

    # 8. POS — Top Selling panel config
    class TopSellingPeriod(models.TextChoices):
        TODAY = 'today', _('Today')
        WEEK  = 'week',  _('This Week')
        MONTH = 'month', _('This Month')
        ALL   = 'all',   _('All Time')

    pos_top_selling_period = models.CharField(
        _("Top Selling Period"), max_length=10,
        choices=TopSellingPeriod.choices, default=TopSellingPeriod.MONTH,
    )
    pos_top_selling_category = models.ForeignKey(
        'inventory.Category', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
        verbose_name=_("Top Selling Category Filter"),
    )
    pos_top_selling_limit = models.PositiveSmallIntegerField(
        _("Top Selling Limit"), default=8,
        help_text=_("Max items shown in the POS Top Selling panel (4–10)."),
    )
    pos_cart_display_fields = models.JSONField(
        _("POS Cart Display Fields"), default=list, blank=True,
        help_text=_("Up to 4 fields shown under each line in the POS cart. "
                    "Tokens: 'category' or 'attr:<attribute_key>'."),
    )

    # 9. Services Module
    service_types = models.JSONField(
        _("Service Types"), default=list, blank=True,
        help_text=_("List of service type labels shown in the service form dropdown."),
    )
    service_notify_hours = models.PositiveSmallIntegerField(
        _("Notify Before (hours)"), default=1,
        help_text=_("Send ETA notification this many hours before the deadline. 0 = disabled."),
    )

    # 10. Printer Names (used by QZ Tray for direct USB/network printing)
    label_printer_name   = models.CharField(_("Label Printer Name"),   max_length=120, blank=True)
    receipt_printer_name = models.CharField(_("Receipt Printer Name"), max_length=120, blank=True)

    # 10b. Print defaults — pre-check the per-transaction print boxes so the
    # cashier never has to tick them every sale/service. (POS = sales receipt,
    # SRV = service receipt. "double" = print two copies, e.g. client + store.)
    pos_print_default        = models.BooleanField(_("POS: print receipt by default"),    default=True)
    pos_double_print_default = models.BooleanField(_("POS: 2x print by default"),          default=False)
    srv_print_default        = models.BooleanField(_("Service: print receipt by default"), default=True)
    srv_double_print_default = models.BooleanField(_("Service: 2x print by default"),      default=True)

    # 10c. Receipt printer output controls (QZ Tray ESC/POS direct-print path)
    receipt_copies   = models.PositiveSmallIntegerField(_("Receipt copies (1–5)"), default=1)
    receipt_auto_cut = models.BooleanField(_("Auto-cut after print"), default=True)
    receipt_cut_feed = models.PositiveSmallIntegerField(_("Cut feed distance mm (0–20)"), default=0)

    # 11. Notification sound — store DEFAULTS. The store admin/owner sets these;
    # a new staff member's per-user prefs are seeded from them, then each user can
    # change their own (NotificationPreference). Values are sound ids 's01'..'s10'
    # or 'mute'. Plain CharFields to avoid a core→notifications import cycle.
    default_info_sound    = models.CharField(max_length=10, default='s01')
    default_warning_sound = models.CharField(max_length=10, default='s02')
    default_alert_sound   = models.CharField(max_length=10, default='s03')

    def __str__(self):
        return f"Settings for {self.store.name}"

# Signal to auto-create settings when a Store is created
from django.db.models.signals import post_save
from django.dispatch import receiver

_DEFAULT_PAYMENT_METHODS = [
    {'name': 'Cash',        'is_cash': True,  'is_agel': False},
    {'name': 'InstaPay',    'is_cash': False, 'is_agel': False},
    {'name': 'E-Wallet',    'is_cash': False, 'is_agel': False},
    {'name': 'Credit Card', 'is_cash': False, 'is_agel': False},
    {'name': 'Ajel',        'is_cash': False, 'is_agel': True},
]


@receiver(post_save, sender=Store)
def create_store_settings(sender, instance, created, **kwargs):
    if created:
        StoreSettings.objects.create(
            store=instance,
            restocking_fee_percent=0,
            return_window_days=0,
            service_types=['Hardware', 'Software', 'HW/SW', 'Apps', 'Unknown'],
            service_notify_hours=1,
        )
        from users.models import Customer
        Customer.objects.get_or_create(
            store=instance, is_walk_in=True,
            defaults={'name': 'Walk-in', 'phone_number': '0000000000'},
        )
        from finance.models import PaymentMethod
        for m in _DEFAULT_PAYMENT_METHODS:
            PaymentMethod.objects.get_or_create(
                store=instance, name=m['name'],
                defaults={'is_cash': m['is_cash'], 'is_agel': m['is_agel']},
            )


class LabelPreset(TimestampedModel):
    """A named label size + field-visibility config for barcode/price label printing."""
    store      = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='label_presets')
    name       = models.CharField(max_length=100)
    width_mm   = models.PositiveSmallIntegerField(default=40)
    height_mm  = models.PositiveSmallIntegerField(default=20)
    show_store_name   = models.BooleanField(default=True)
    show_product_name = models.BooleanField(default=True)
    show_sku          = models.BooleanField(default=True)
    show_barcode      = models.BooleanField(default=True)
    show_price        = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ['-is_default', 'name']

    def __str__(self):
        return f"{self.name} ({self.width_mm}×{self.height_mm}mm)"


class DashboardLayout(TimestampedModel):
    """Which dashboard widgets are shown, and in what order.

    `store = NULL` is the **platform-wide global default**, set only by sudo —
    every store renders it. Per-store rows (a `store` FK set) will override the
    global for that store later; that layer isn't built yet, but the structure
    is here so it can drop in without a schema change.
    """
    store = models.OneToOneField(
        Store, null=True, blank=True, on_delete=models.CASCADE,
        related_name='dashboard_layout',
    )
    # Ordered list of widget ids (see core/dashboard_widgets.py), capped at MAX_WIDGETS.
    selected_widgets = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name = "Dashboard layout"

    @classmethod
    def get_global(cls):
        """The single sudo-managed global row, seeded with the defaults once."""
        from core.dashboard_widgets import DEFAULT_WIDGETS
        obj, _ = cls.objects.get_or_create(
            store=None, defaults={'selected_widgets': list(DEFAULT_WIDGETS)},
        )
        return obj

    def __str__(self):
        scope = 'global' if self.store_id is None else f'store:{self.store_id}'
        return f"DashboardLayout({scope}, {len(self.selected_widgets or [])} widgets)"
