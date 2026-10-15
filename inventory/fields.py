"""Serializer fields shared by every form that names a product variant by id."""
from rest_framework import serializers


class ShopVariantField(serializers.PrimaryKeyRelatedField):
    """A variant of the CALLER'S shop only — another shop's id answers "does not exist" (s175 security review: the sale
    cart, returns, stock corrections and transfers took any shop's variant, because ProductVariant's default manager
    isn't tenant-scoped). Resolved per request: a nested field only gets its context once bound.

    No request in the context (internal code building the serializer itself) keeps the old behaviour; a request
    without a shop (sudo) gets nothing."""

    def get_queryset(self):
        from inventory.models import ProductVariant
        request = self.context.get('request')
        if request is None:
            return ProductVariant.objects.all()
        store = getattr(getattr(request, 'user', None), 'store', None)
        if store is None:
            return ProductVariant.objects.none()
        return ProductVariant.objects.filter(store=store)
