"""Typesense client + collection config for Vendorya search (§SEARCH-TS).

ONE shared Typesense instance runs on this box (localhost:8108) and serves BOTH
the dev and prod Django processes. Because the dev DB is a clone of prod (identical
store UUIDs), the two environments MUST NOT share a collection — TYPESENSE_COLLECTION
namespaces them apart (products_dev / products_prod).

Design: ONE `products` collection, every document tagged with `store_id` + `source`.
This mirrors the existing Django autocomplete query exactly (filter by store, union
STORE + MEMORY_BASE), so store isolation and both autocomplete-source modes are a
single `filter_by` away — no per-store collections, no Memory-Base refactor.

Everything here is best-effort. If Typesense is unconfigured or unreachable the
caller falls back to the pg_trgm query — search can never break because of Typesense.
"""
import os

import typesense
from typesense.exceptions import ObjectNotFound

HOST       = os.environ.get('TYPESENSE_HOST', '127.0.0.1')
PORT       = os.environ.get('TYPESENSE_PORT', '8108')
PROTOCOL   = os.environ.get('TYPESENSE_PROTOCOL', 'http')
API_KEY    = os.environ.get('TYPESENSE_API_KEY', '')
COLLECTION = os.environ.get('TYPESENSE_COLLECTION', 'products_dev')

# Document schema. Fields searched: name (EN trade name), brand_ar (Arabic trade
# name), active_ing / active_ing_ar (active ingredient). store_id + source are
# filter facets; source_rank breaks _text_match ties to surface STORE above MB.
SCHEMA = {
    'name': COLLECTION,
    'fields': [
        {'name': 'store_id',      'type': 'string', 'facet': True},
        {'name': 'source',        'type': 'string', 'facet': True},
        {'name': 'source_rank',   'type': 'int32'},
        {'name': 'name',          'type': 'string'},
        {'name': 'brand_ar',      'type': 'string', 'optional': True},
        {'name': 'active_ing',    'type': 'string', 'optional': True},
        {'name': 'active_ing_ar', 'type': 'string', 'optional': True},
    ],
}

_clients = {}


def is_configured():
    """True only when an API key is present — gates every Typesense call."""
    return bool(API_KEY)


def get_client(timeout=2):
    """Cached client with the given connection timeout (seconds).

    Short (2s) for the request/search path; long (e.g. 120s) for bulk reindex.
    """
    if timeout not in _clients:
        _clients[timeout] = typesense.Client({
            'nodes': [{'host': HOST, 'port': str(PORT), 'protocol': PROTOCOL}],
            'api_key': API_KEY,
            'connection_timeout_seconds': timeout,
        })
    return _clients[timeout]


def collection_exists(client=None):
    client = client or get_client()
    try:
        client.collections[COLLECTION].retrieve()
        return True
    except ObjectNotFound:
        return False


def ensure_collection(client=None):
    """Create the collection if missing. Idempotent."""
    client = client or get_client()
    if not collection_exists(client):
        client.collections.create(SCHEMA)


def search_ids(store_id, q, store_history=False, limit=20):
    """Return ranked product IDs (strings) for the query.

    Typo-tolerant + prefix (so 'convantin' → 'Conventin'). Filters by store, and
    by source=STORE in store_history mode. Raises on ANY Typesense error — the
    caller MUST catch and fall back to pg_trgm.
    """
    filter_by = f'store_id:={store_id}'
    if store_history:
        filter_by += ' && source:=STORE'
    res = get_client(timeout=2).collections[COLLECTION].documents.search({
        'q': q,
        'query_by': 'name,brand_ar,active_ing,active_ing_ar',
        'query_by_weights': '4,3,2,1',
        'filter_by': filter_by,
        'sort_by': '_text_match:desc,source_rank:asc',
        'prefix': True,
        'num_typos': 2,
        'per_page': limit,
        'include_fields': 'id',
    })
    return [hit['document']['id'] for hit in res.get('hits', [])]
