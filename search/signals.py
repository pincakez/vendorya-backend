"""Keep the Typesense index in sync with Product writes (§SEARCH-TS).

Both handlers call the fail-safe indexing helpers (which swallow every Typesense
error), so a Typesense outage can never roll back or block a product save/delete.
Soft delete is a save with is_deleted=True → the product is dropped from the index.
"""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from inventory.models import Product
from . import indexing


@receiver(post_save, sender=Product)
def _product_saved(sender, instance, **kwargs):
    if getattr(instance, 'is_deleted', False):
        indexing.delete_product(instance.pk)
    else:
        indexing.upsert_product(instance)


@receiver(post_delete, sender=Product)
def _product_deleted(sender, instance, **kwargs):
    indexing.delete_product(instance.pk)
