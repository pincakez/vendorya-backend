"""Finding a product by its CODE — SKU, SKU2 or barcode (s159).

- An EXACT code always wins: a short code like `12077` is also inside longer codes, so a scan must
  never land on "a product that merely contains it" (TODO §PROLINE-WALLS).
- Dashes don't matter when typed: `12345667` finds SKU `123-456-67` (SKU setup with dashes on).
- SKU2 is searched only while the shop's SKU2 is ON. "Hide in search" only hides it from the result
  rows on screen — typing or scanning it still finds the product (Yakot).
- A pack/box barcode (ProductUnit.barcode) finds its product too; `scan_unit_id` tells the POS which
  pack was scanned, so it adds 1 pack, not 1 piece.
"""
from django.db.models import Exists, OuterRef, Q, Value
from django.db.models.functions import Replace

from inventory.models import ProductUnit, ProductVariant


def _variants():
    return (ProductVariant.objects.filter(product=OuterRef('pk'))
            .annotate(sku_flat=Replace('sku', Value('-'), Value(''))))


def _units():
    return ProductUnit.objects.filter(variant__product=OuterRef('pk'), variant__is_deleted=False)


def _clean(q):
    return (q or '').strip(), (q or '').strip().replace('-', '')


def exact_code(q, sku2=False, packs=True):
    """Products with a variant whose SKU / SKU2 / barcode IS `q` (dashes ignored), or — with
    `packs` — a live pack unit whose barcode IS `q`."""
    raw, flat = _clean(q)
    if not raw:
        return Q(pk__in=[])
    cond = Q(barcode=raw) | Q(sku=raw)
    if flat:
        cond |= Q(sku_flat=flat)
        if sku2:
            cond |= Q(sku2=flat)
    found = Q(Exists(_variants().filter(cond)))
    if packs:
        found |= Q(Exists(_units().filter(barcode=raw)))
    return found


def code_contains(q, sku2=False, packs=True):
    """Products with a variant whose SKU / SKU2 / barcode CONTAINS `q` (dashes ignored), or — with
    `packs` — a live pack unit whose barcode contains it."""
    raw, flat = _clean(q)
    if not raw:
        return Q(pk__in=[])
    cond = Q(barcode__icontains=raw)
    if flat:
        cond |= Q(sku_flat__icontains=flat)
        if sku2:
            cond |= Q(sku2__icontains=flat)
    found = Q(Exists(_variants().filter(cond)))
    if packs:
        found |= Q(Exists(_units().filter(barcode__icontains=raw)))
    return found


def scan_unit_id(selling_units, q):
    """The pack the code `q` was scanned from, read off a product's serialized `selling_units`
    (so it obeys the shop's multi-unit switch, weight mode and the per-pack "sellable" flag).
    None when `q` is not a sellable pack's barcode — the caller then adds the base unit as before."""
    raw = (q or '').strip()
    if not raw:
        return None
    for u in selling_units or ():
        if not u.get('is_base') and u.get('sellable', True) and (u.get('barcode') or '').strip() == raw:
            return u.get('id')
    return None


def barcode_owner(store, barcode, skip_variant_ids=(), skip_unit_ids=()):
    """Name of the live STORE product in `store` that already uses `barcode` — as its own barcode
    or a pack's — else None. A barcode is unique per shop (variants AND packs together), so a scan
    can only ever mean one thing. Soft-deleted rows don't count, other shops may reuse it, and the
    Memory Base reference pool is ignored (its entries are not sellable). Empty never clashes."""
    from inventory.models import Product
    raw = (barcode or '').strip()
    if not raw or store is None:
        return None
    hit = (ProductVariant.objects
           .filter(barcode=raw, product__store=store, product__is_deleted=False,
                   product__source=Product.Source.STORE)
           .exclude(id__in=list(skip_variant_ids))
           .values_list('product__name', flat=True).first())
    if hit is not None:
        return hit
    return (ProductUnit.objects
            .filter(barcode=raw, variant__is_deleted=False, variant__product__store=store,
                    variant__product__is_deleted=False, variant__product__source=Product.Source.STORE)
            .exclude(id__in=list(skip_unit_ids))
            .values_list('variant__product__name', flat=True).first())
