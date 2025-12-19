import uuid
from django.db import models
from django.conf import settings
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
class Store(TimestampedModel, SoftDeleteModel):
    class SubscriptionPlan(models.TextChoices):
        FREE = 'FREE', _('Free')
        PREMIUM = 'PREMIUM', _('Premium')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(_("Store Name"), max_length=200)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='owned_stores', on_delete=models.CASCADE)
    plan = models.CharField(max_length=20, choices=SubscriptionPlan.choices, default=SubscriptionPlan.FREE)
    is_active = models.BooleanField(default=True)
    
    # Defaults
    default_supplier = models.ForeignKey('inventory.Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    default_category = models.ForeignKey('inventory.Category', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    default_language = models.CharField(max_length=5, default='ar')
    currency_symbol = models.CharField(max_length=10, default='EGP')

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

    class Meta:
        verbose_name = _("Branch")
        verbose_name_plural = _("Branches")

    def __str__(self):
        return self.name