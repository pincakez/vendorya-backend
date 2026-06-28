"""Keep the Typesense index in sync with Product writes (§SEARCH-TS).

Both handlers call the fail-safe indexing helpers (which swallow every Typesense
error), so a Typesense outage can never roll back or block a product save/delete.
Soft delete is a save with is_deleted=True → the product is dropped from the index.
"""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from inventory.models import Product, ProductAttribute
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


# Indexed fields brand_ar / active_ing / active_ing_ar live on ProductAttribute,
# which is written AFTER the Product (on its variant) — so a product's first save
# indexes an empty Arabic doc. Re-index the parent product whenever an attribute
# changes so Arabic search finds a drug the moment it's created / received / edited.
@receiver(post_save, sender=ProductAttribute)
@receiver(post_delete, sender=ProductAttribute)
def _attribute_changed(sender, instance, **kwargs):
    product = getattr(getattr(instance, 'variant', None), 'product', None)
    if product is None:
        return
    if getattr(product, 'is_deleted', False):
        indexing.delete_product(product.pk)
    else:
        indexing.upsert_product(product)
