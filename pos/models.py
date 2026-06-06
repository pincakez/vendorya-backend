import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import Store


class POSFavoriteItem(models.Model):
    """Store-level curated quick-access products shown in the POS left panel. Max 10 per store."""
    id      = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store   = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='pos_favorites')
    product = models.ForeignKey('inventory.Product', on_delete=models.CASCADE, related_name='+')
    order   = models.PositiveSmallIntegerField(default=0)

    class Meta:
        unique_together = ('store', 'product')
        ordering = ['order']

    def __str__(self):
        return f"{self.store.name} — {self.product.name} (#{self.order})"
