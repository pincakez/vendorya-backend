"""SKU + SKU2 numbering engine (s159, Yakot 2026-10-07 — TODO §SKU-SETUP + §SKU2).

Our SKU = product number + supplier code + shop code, each part's width set per shop by sudo on
Admin → SYSTEM → SKU Management (`core/sku_admin.py`), optionally joined by dashes (123-45-67).
Defaults (4 + 3 + store_code, no dashes) make exactly the 10-digit SKUs every shop made before.

The rules Yakot signed off:
- A width GROWS by itself when its numbers run out (product 999 on 3 digits → 1000). Only NEW codes
  get the extra digit; an existing SKU never changes (stickers are already printed).
- Unique PER SHOP. Without dashes two different splits can spell the same digits (123+456+67 and
  1234+56+67 both read 12345667) → on a clash the number is SKIPPED and the next free one is taken.
- The parts are stored on the variant (`sku_number`, `sku_prefix`); the next number is counted from
  them, never parsed back out of the SKU text.
"""
import random

from django.db import transaction

#: The widest a part may grow. Past this the shop has genuinely run out (≈ a million products per supplier).
MAX_PRODUCT_DIGITS = 6
MAX_SUPPLIER_DIGITS = 4
SKU2_MAX_DIGITS = 8


def _settings(store):
    # Always fresh from the DB: a cached `store.settings` can be older than a sudo change made
    # in the meantime (one small query, only when a code is made or searched).
    from core.models import StoreSettings
    s, _ = StoreSettings.objects.get_or_create(store_id=store.pk)
    return s


def shop_code(store):
    return _settings(store).sku_shop_code or store.store_code or ''


def assemble(number, width, supplier_code, shop, dashes):
    head = f"{number:0{width}d}"
    return '-'.join((head, supplier_code, shop)) if dashes else f"{head}{supplier_code}{shop}"


def next_sku(product):
    """→ (sku, sku_number, sku_prefix) for a new variant of `product`. Call inside a transaction."""
    from inventory.models import ProductVariant, Supplier

    store, supplier = product.store, product.supplier
    shop = shop_code(store)
    if not shop:
        raise ValueError("Store must have a store_code set before products can be created.")
    if not supplier:
        raise ValueError("Product must be assigned a supplier before a variant can be saved.")
    if not supplier.prefix_locked:
        raise ValueError("Supplier prefix must be confirmed (locked) before products can be created.")

    st = _settings(store)
    digits, dashes = st.sku_product_digits, st.sku_dashes
    sup = supplier.code_prefix

    with transaction.atomic():
        # Lock the supplier row — two products of one supplier can't take the same number.
        Supplier.all_objects.select_for_update().get(pk=supplier.pk)
        variants = ProductVariant.all_objects.filter(store=store)
        used = set(variants.filter(sku_prefix=sup, sku_number__isnull=False)
                   .values_list('sku_number', flat=True))

        def free(candidate):
            return not variants.filter(sku=candidate).exists()

        if st.product_numbering_mode == 'RANDOM':
            width = digits
            while width <= MAX_PRODUCT_DIGITS:
                low = 1 if width == digits else 10 ** (width - 1)
                pool = [n for n in range(low, 10 ** width) if n not in used]
                random.shuffle(pool)
                for n in pool:
                    sku = assemble(n, width, sup, shop, dashes)
                    if free(sku):
                        return sku, n, sup
                width += 1
        else:
            n = max(used, default=0) + 1
            while len(str(n)) <= MAX_PRODUCT_DIGITS:
                width = max(digits, len(str(n)))
                sku = assemble(n, width, sup, shop, dashes)
                if free(sku):
                    return sku, n, sup
                n += 1          # clash with another split → skip this number (Yakot OK'd)
    raise ValueError("No free product number left for this supplier.")


# ── Supplier codes ──────────────────────────────────────────────────────────────────────────

def supplier_code_width(store):
    """The width a NEW supplier's code must have: the shop's setting, plus one digit for every
    width that is completely used up."""
    from inventory.models import Supplier
    width = _settings(store).sku_supplier_digits
    taken = list(Supplier.all_objects.filter(store=store).values_list('code_prefix', flat=True))
    while width < MAX_SUPPLIER_DIGITS and sum(1 for c in taken if len(c) == width) >= 10 ** width - _lowest(width):
        width += 1
    return width


def _lowest(width):
    # 2-digit codes run 10–99 and 3-digit 100–999 (a leading zero reads like a typo on a sticker);
    # existing 3-digit shops already follow 100–999.
    return 10 ** (width - 1)


def next_free_supplier_code(store):
    from inventory.models import Supplier
    width = supplier_code_width(store)
    taken = set(Supplier.all_objects.filter(store=store).values_list('code_prefix', flat=True))
    for n in range(_lowest(width), 10 ** width):
        if str(n) not in taken:
            return str(n)
    return None


# ── SKU2 ────────────────────────────────────────────────────────────────────────────────────

def sku2_active(store):
    from core.models import StoreSettings
    return _settings(store).sku2_state == StoreSettings.Sku2State.ON


def next_sku2(store):
    """A SKU2 for a NEW product — only when the shop has SKU2 ON and "for new products" ticked.
    Sequence continues after the highest numeric SKU2 (e.g. the old system ended at 12077 → 12078)."""
    from inventory.models import ProductVariant
    st = _settings(store)
    if not (sku2_active(store) and st.sku2_auto_new):
        return None
    variants = ProductVariant.all_objects.filter(store=store, sku2__isnull=False)
    used = set(variants.values_list('sku2', flat=True))
    digits = st.sku2_digits
    if st.sku2_mode == 'RANDOM':
        width = digits
        while width <= SKU2_MAX_DIGITS:
            pool = [n for n in range(10 ** (width - 1), 10 ** width) if f"{n:0{width}d}" not in used]
            if pool:
                return f"{random.choice(pool):0{width}d}"
            width += 1
        raise ValueError("No free SKU2 left.")
    top = max((int(c) for c in used if c.isdigit()), default=0)
    n = top + 1
    while len(str(n)) <= SKU2_MAX_DIGITS:
        code = f"{n:0{max(digits, len(str(n)))}d}"
        if code not in used:
            return code
        n += 1
    raise ValueError("No free SKU2 left.")
