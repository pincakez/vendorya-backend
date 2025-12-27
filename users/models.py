import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _
from core.models import Address, TimestampedModel, SoftDeleteModel, Store
class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER = 'OWNER', _('Owner')
        ADMIN = 'ADMIN', _('Admin')
        MANAGER = 'MANAGER', _('Manager')
        CASHIER = 'CASHIER', _('Cashier')

    store = models.ForeignKey(Store, related_name='staff', on_delete=models.CASCADE, null=True, blank=True)
    role = models.CharField(_("Role"), max_length=50, choices=Role.choices, default=Role.CASHIER)
    photo = models.ImageField(_("User Photo"), upload_to='user_photos/', blank=True, null=True)

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

    class Meta:
        verbose_name = _("Customer")
        verbose_name_plural = _("Customers")
        unique_together = ('store', 'phone_number')
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.phone_number})"