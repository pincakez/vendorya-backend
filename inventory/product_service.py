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
                                description='', extra_product_fields=None,
                                auto_mb_units=False):
    """Create a Product and its default variant in one atomic step.

    - `cost_price` → variant.cost_price (what you paid)
    - `sell_price` → variant.sell_price (retail). Falls back to base_price when 0.
    - `attributes` → list of {definition|definition_id, value} on the default variant.
    - `auto_mb_units` → §MU-MB: when True (the New Purchase onboarding flow, which has
      no selling-units UI), silently link the product to its drug reference by name and
      seed its Strip/Pack tiers from the enriched packaging — mirroring the front-end
      auto-fill the New Product modal does. Gated on the store's respect-MB + multi-unit
      settings.
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
    # entry that quietly feeds the autofill. Gated on store.settings.mb_auto_register
    # (default True). Best-effort + isolated so a hiccup never breaks product creation.
    try:
        if getattr(store.settings, 'mb_auto_register', True):
            with transaction.atomic():
                register_memory_base_entry(store, name=name, attributes=attributes)
    except Exception:
        pass

    # §MU-MB: seed POS packaging tiers from the drug reference for the New Purchase
    # flow. Best-effort + isolated — never break product creation over it.
    if auto_mb_units:
        try:
            apply_mb_units(store, product)
        except Exception:
            pass
    return product


def apply_mb_units(store, product):
    """Link a freshly-created product to its DrugProfile (matched by name) and, when
    the store opts in (pos_respect_mb_units + multi-unit on), build its Strip + Pack
    selling-unit tiers from the enriched packaging. This is the New Purchase equivalent
    of the New Product modal's front-end auto-fill (that flow has no units UI). No-op
    when there's no name match, the store hasn't opted in, packaging data is missing,
    or the variant already has selling units.
    """
    from .models import DrugProfile, ProductUnit, is_multi_unit_enabled

    dp = DrugProfile.objects.filter(name__iexact=(product.name or '').strip()).first()
    if not dp:
        return
    if product.drug_profile_id is None:
        product.drug_profile = dp
        product.save(update_fields=['drug_profile'])

    settings = getattr(store, 'settings', None)
    if not settings or not getattr(settings, 'pos_respect_mb_units', False):
        return
    if not is_multi_unit_enabled(store.id):
        return

    tps = dp.tablets_per_strip or 0
    spp = dp.strips_per_pack or 0
    if tps <= 0 or spp <= 0:
        return

    variant = product.variants.first()
    if not variant or variant.selling_units.exists():
        return

    tier_names = settings.unit_tier_names or ['Strip', 'Pack']
    strip_name = tier_names[0] if len(tier_names) > 0 else 'Strip'
    pack_name  = tier_names[1] if len(tier_names) > 1 else 'Pack'
    ProductUnit.objects.create(variant=variant, name=strip_name, factor=tps,       sell_price=0, sort_order=1)
    ProductUnit.objects.create(variant=variant, name=pack_name,  factor=tps * spp, sell_price=0, sort_order=2)

    # 2 tiers → the base unit isn't individually sellable; 3 tiers → it is.
    tier_count = getattr(settings, 'pos_tier_count', 3) or 3
    product.sell_base_unit = tier_count >= 3
    product.save(update_fields=['sell_base_unit'])


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


def dedup_memory_base_for_store(store):
    """Collapse duplicate Memory Base entries (same case-insensitive name) within a
    store (superfix §2.6). For each duplicated name we keep the *richest* entry —
    the one carrying the most attributes, oldest wins a tie — and soft-delete the
    rest (recoverable via admin Trash). Returns the number of entries removed.
    Idempotent: a second run removes nothing. Backs the manual "Remove duplicates"
    button and the nightly `dedup_memory_base` management command.
    """
    from collections import defaultdict
    groups = defaultdict(list)
    qs = (Product.objects.filter(store=store, source=Product.Source.MEMORY_BASE)
          .prefetch_related('variants__attributes'))
    for p in qs:
        key = (p.name or '').strip().lower()
        if key:
            groups[key].append(p)

    def _attr_count(p):
        return sum(len(v.attributes.all()) for v in p.variants.all())

    removed = 0
    with transaction.atomic():
        for items in groups.values():
            if len(items) < 2:
                continue
            items.sort(key=lambda p: (-_attr_count(p), p.created_at))
            for dup in items[1:]:
                dup.delete()  # soft-delete (SoftDeleteModel) — keeps it recoverable
                removed += 1
    return removed
