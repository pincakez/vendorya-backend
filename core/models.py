import uuid
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _

class TimestampedModel(models.Model):
    """Abstract base class with self-updating created_at/updated_at fields."""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ['-updated_at', '-created_at']

class Store(TimestampedModel):
    """Represents a tenant (Store)."""
    class SubscriptionPlan(models.TextChoices):
        FREE = 'FREE', _('Free')
        PREMIUM = 'PREMIUM', _('Premium')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(_("Store Name"), max_length=200)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='owned_stores', on_delete=models.CASCADE)
    plan = models.CharField(max_length=20, choices=SubscriptionPlan.choices, default=SubscriptionPlan.FREE)
    is_active = models.BooleanField(default=True)
    
    # We use a string reference here to avoid circular imports with Inventory app
    default_supplier = models.ForeignKey(
        'inventory.Supplier',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    
    default_language = models.CharField(max_length=5, default='ar')
    currency_symbol = models.CharField(max_length=10, default='EGP')

    class Meta:
        verbose_name = _("Store")
        verbose_name_plural = _("Stores")

    def __str__(self):
        return self.name

class Address(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='addresses')
    street_1 = models.CharField(_("Street Address 1"), max_length=255)
    street_2 = models.CharField(_("Street Address 2"), max_length=255, blank=True, null=True)
    city = models.CharField(_("City"), max_length=100)
    country = models.CharField(_("Country"), max_length=100, default=_("Egypt"))

    def __str__(self):
        return f"{self.street_1}, {self.city}"

class Branch(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='branches')
    name = models.CharField(_("Branch Name"), max_length=150)
    address = models.OneToOneField(Address, on_delete=models.PROTECT, verbose_name=_("Address"))
    is_main_branch = models.BooleanField(default=False)

    def __str__(self):
        return self.name