"""Finding a product by its CODE — SKU, SKU2 or barcode (s159).

- An EXACT code always wins: a short code like `12077` is also inside longer codes, so a scan must
  never land on "a product that merely contains it" (TODO §PROLINE-WALLS).
- Dashes don't matter when typed: `12345667` finds SKU `123-456-67` (SKU setup with dashes on).
- SKU2 is searched only while the shop's SKU2 is ON. "Hide in search" only hides it from the result
  rows on screen — typing or scanning it still finds the product (Yakot).
"""
from django.db.models import Exists, OuterRef, Q, Value
from django.db.models.functions import Replace

from inventory.models import ProductVariant


def _variants():
    return (ProductVariant.objects.filter(product=OuterRef('pk'))
            .annotate(sku_flat=Replace('sku', Value('-'), Value(''))))


def _clean(q):
    return (q or '').strip(), (q or '').strip().replace('-', '')


def exact_code(q, sku2=False):
    """Products with a variant whose SKU / SKU2 / barcode IS `q` (dashes ignored)."""
    raw, flat = _clean(q)
    if not raw:
        return Q(pk__in=[])
    cond = Q(barcode=raw) | Q(sku=raw)
    if flat:
        cond |= Q(sku_flat=flat)
        if sku2:
            cond |= Q(sku2=flat)
    return Exists(_variants().filter(cond))


def code_contains(q, sku2=False):
    """Products with a variant whose SKU / SKU2 / barcode CONTAINS `q` (dashes ignored)."""
    raw, flat = _clean(q)
    if not raw:
        return Q(pk__in=[])
    cond = Q(barcode__icontains=raw)
    if flat:
        cond |= Q(sku_flat__icontains=flat)
        if sku2:
            cond |= Q(sku2__icontains=flat)
    return Exists(_variants().filter(cond))
