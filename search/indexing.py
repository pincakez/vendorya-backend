"""Build + sync Typesense documents for products (§SEARCH-TS).

Single-doc helpers (used by signals) are fail-safe — they swallow every error so
indexing can NEVER roll back or block a product write. The bulk reindex is used by
the ts_reindex management command and is allowed to raise (run interactively).
"""
import logging

from typesense.exceptions import ObjectNotFound

from inventory.models import Product
from . import client as ts

logger = logging.getLogger(__name__)

# Variant attribute keys indexed for Arabic + active-ingredient search.
_ATTR_KEYS = ('brand_ar', 'active_ing', 'active_ing_ar')


def build_document(product):
    """Map a Product (+ its variant attributes) to a Typesense document.

    Aggregates the indexed attribute values across the product's variants (first
    non-empty wins). For bulk use, prefetch variants__attributes__definition.
    """
    attrs = {}
    for variant in product.variants.all():
        for a in variant.attributes.all():
            key = a.definition.key
            if key in _ATTR_KEYS and key not in attrs and a.value:
                attrs[key] = a.value
    return {
        'id':            str(product.id),
        'store_id':      str(product.store_id),
        'source':        product.source,
        'source_rank':   0 if product.source == Product.Source.STORE else 1,
        'name':          product.name or '',
        'brand_ar':      attrs.get('brand_ar', ''),
        'active_ing':    attrs.get('active_ing', ''),
        'active_ing_ar': attrs.get('active_ing_ar', ''),
    }


def upsert_product(product):
    """Index/refresh one product. Best-effort — never raises."""
    if not ts.is_configured():
        return
    try:
        ts.get_client().collections[ts.COLLECTION].documents.upsert(build_document(product))
    except Exception as exc:           # noqa: BLE001 — indexing must never break a write
        logger.warning("Typesense upsert failed for product %s: %s", product.pk, exc)


def delete_product(product_id):
    """Remove one product from the index. Best-effort — never raises."""
    if not ts.is_configured():
        return
    try:
        ts.get_client().collections[ts.COLLECTION].documents[str(product_id)].delete()
    except Exception as exc:           # noqa: BLE001
        logger.warning("Typesense delete failed for product %s: %s", product_id, exc)


def reindex_all(stdout=None, batch_size=2000):
    """Drop + rebuild the whole collection from the DB. Idempotent.

    Indexes every ACTIVE product (STORE + MEMORY_BASE) across ALL stores. Returns
    (total_imported, failed_count). May raise — run from ts_reindex only.
    """
    client = ts.get_client(timeout=120)
    try:
        client.collections[ts.COLLECTION].delete()
    except ObjectNotFound:
        pass
    client.collections.create(ts.SCHEMA)

    total, failed = 0, 0

    def flush(batch):
        nonlocal failed
        results = client.collections[ts.COLLECTION].documents.import_(batch, {'action': 'upsert'})
        failed += sum(1 for r in results if not r.get('success', False))

    qs = (Product.all_objects.filter(is_deleted=False)
          .prefetch_related('variants', 'variants__attributes', 'variants__attributes__definition'))
    batch = []
    for product in qs.iterator(chunk_size=batch_size):
        batch.append(build_document(product))
        if len(batch) >= batch_size:
            flush(batch)
            total += len(batch)
            batch = []
            if stdout:
                stdout(f"  indexed {total}…")
    if batch:
        flush(batch)
        total += len(batch)
    return total, failed
