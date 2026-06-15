"""Shared product-creation logic.

Single source of truth for "create a Product + its default ProductVariant +
attributes" so the Products page (ProductWriteSerializer) and the Purchases
onboarding flow can't drift apart. SKU generation happens automatically in
ProductVariant.save() (which requires a locked supplier).
"""
from django.db import transaction

from .models import Product, ProductVariant, ProductAttribute, AttributeDefinition


def create_product_with_variant(store, *, name, supplier=None, category=None,
                                base_price=0, cost_price=0, sell_price=0,
                                attributes=None, reorder_level=None,
                                description='', extra_product_fields=None):
    """Create a Product and its default variant in one atomic step.

    - `cost_price` → variant.cost_price (what you paid)
    - `sell_price` → variant.sell_price (retail). Falls back to base_price when 0.
    - `attributes` → list of {definition|definition_id, value} on the default variant.
    Returns the Product. Raises ValueError from SKU generation if `supplier` is
    missing / unlocked (caller decides how to handle that).
    """
    attributes = attributes or []
    product_fields = {
        'store': store,
        'name': name,
        'supplier': supplier,
        'category': category,
        'base_price': base_price,
        'description': description or '',
    }
    if extra_product_fields:
        product_fields.update(extra_product_fields)

    with transaction.atomic():
        product = Product.objects.create(**product_fields)
        variant = ProductVariant.objects.create(
            product=product,
            cost_price=cost_price,
            sell_price=sell_price or base_price or 0,
            **({'reorder_level': reorder_level} if reorder_level is not None else {}),
        )
        for attr in attributes:
            defn_id = attr.get('definition') or attr.get('definition_id')
            value = attr.get('value', '')
            if defn_id and value:
                defn = AttributeDefinition.objects.filter(
                    id=defn_id, store=store,
                ).first()
                if defn:
                    ProductAttribute.objects.create(
                        variant=variant, definition=defn, value=value,
                    )
    return product
