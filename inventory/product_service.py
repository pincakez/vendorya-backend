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

    # Auto-register into the Memory Base reference pool (superfix §2.4): every
    # STORE product the store creates also accumulates a supplier-less reference
    # entry that quietly feeds the autofill. Best-effort + isolated in its own
    # savepoint so a bookkeeping hiccup can never break real product creation.
    try:
        with transaction.atomic():
            register_memory_base_entry(store, name=name, attributes=attributes)
    except Exception:
        pass
    return product


def register_memory_base_entry(store, *, name, attributes=None):
    """Ensure a Memory Base reference entry exists for `name` in this store.

    The Memory Base is a supplier-less, SKU-less reference pool that feeds the
    New Purchase / New Product autofill. We call this whenever a STORE product is
    created so the pool accumulates everything the store ever touches. Dedup'd by
    case-insensitive name — a no-op when an entry already exists (returns it).
    Returns the MEMORY_BASE Product (or None for a blank name).
    """
    name = (name or '').strip()
    if not name:
        return None
    existing = Product.objects.filter(
        store=store, source=Product.Source.MEMORY_BASE, name__iexact=name,
    ).first()
    if existing:
        return existing
    mb = Product.objects.create(
        store=store, name=name, source=Product.Source.MEMORY_BASE,
        supplier=None, category=None, base_price=0,
    )
    variant = ProductVariant.objects.create(product=mb, cost_price=0, sell_price=0)
    for attr in (attributes or []):
        defn_id = attr.get('definition') or attr.get('definition_id')
        value = attr.get('value', '')
        if defn_id and value:
            defn = AttributeDefinition.objects.filter(id=defn_id, store=store).first()
            if defn:
                ProductAttribute.objects.create(variant=variant, definition=defn, value=value)
    return mb
