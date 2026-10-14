"""Category spec sets (PROLINE-PARITY Upgrade 9, s168) — the one place that reads and writes them.

A category carries an ordered list of the shop's AttributeDefinitions (`CategorySpec` rows). A
sub-category inherits its parents' lines, root first, then adds its own. The product form asks for
the merged list; the label print shows the values in that order on one line.
"""
import uuid

from django.db import connection, transaction
from rest_framework.exceptions import ValidationError

from .models import AttributeDefinition, Category, CategorySpec, MAX_CATEGORY_DEPTH
from .spec_presets import SPEC_PRESETS

MAX_SPECS_PER_CATEGORY = 40
_SPEC_LOCK_NS = 7312   # advisory-lock namespace: "spec-set writes of one shop" (barcodes use 7311)


def _lock_shop_specs(store):
    """Serialise spec-set writes of ONE shop until the transaction ends, so two preset clicks at the
    same instant can't both create the same attribute key. Postgres only; no-op elsewhere."""
    if connection.vendor != 'postgresql' or not connection.in_atomic_block:
        return
    with connection.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(%s, hashtext(%s))', [_SPEC_LOCK_NS, str(store.pk)])


def category_chain(category):
    """[root, …, category] — the category and its live ancestors, root first."""
    chain, node, guard = [], category, 0
    while node is not None and guard <= MAX_CATEGORY_DEPTH + 1:
        if not node.is_deleted:
            chain.insert(0, node)
        node = node.parent
        guard += 1
    return chain


def _spec_dict(row, inherited_from):
    d = row.definition
    return {
        'id':           str(row.id),
        'definition':   str(d.id),
        'name':         d.name,
        'key':          d.key,
        'input_type':   d.input_type,
        'options':      d.options or [],
        'unit':         d.unit or '',
        'required':     row.required,
        'inherited_from': ({'id': str(inherited_from.id), 'name': inherited_from.name}
                           if inherited_from is not None else None),
    }


def effective_spec_set(category):
    """The merged, ordered spec set for `category`: every live line of its ancestors (root first, each
    category's own order), then its own. A definition listed twice appears once, where it first
    appears, and is required if ANY level requires it. Lines whose attribute was deleted are skipped."""
    if category is None:
        return []
    chain = category_chain(category)
    if not chain:
        return []
    rows = (CategorySpec.all_objects
            .filter(store_id=category.store_id, category_id__in=[c.id for c in chain],
                    is_deleted=False, definition__is_deleted=False)
            .select_related('definition')
            .order_by('sort_order', 'created_at'))
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r.category_id, []).append(r)
    out, seen = [], {}
    for c in chain:
        for r in by_cat.get(c.id, []):
            key = r.definition_id
            if key in seen:
                if r.required:
                    seen[key]['required'] = True
                continue
            d = _spec_dict(r, None if c.id == category.id else c)
            seen[key] = d
            out.append(d)
    return out


def own_spec_rows(category):
    """This category's own live lines (not the inherited ones), in order."""
    return list(CategorySpec.all_objects
                .filter(category=category, is_deleted=False, definition__is_deleted=False)
                .select_related('definition')
                .order_by('sort_order', 'created_at'))


def spec_set_payload(category):
    """What the category screen + product form read.
    - `effective`: the merged list (each attribute once, required if any level requires it);
    - `own`: ALWAYS every own row of this category, in its order — even one a parent also carries
      (flagged `also_inherited`), so saving the own list can never silently drop a row (review fix);
    - `inherited`: the parents' lines (an attribute the category also owns appears in both)."""
    effective = effective_spec_set(category)
    own_rows = own_spec_rows(category)
    parent_ids = set()
    if category.parent_id:
        parent_ids = {s['definition'] for s in effective_spec_set(category.parent)}
    own = []
    for r in own_rows:
        d = _spec_dict(r, None)
        d['also_inherited'] = str(r.definition_id) in parent_ids
        own.append(d)
    return {
        'category': str(category.id),
        'effective': effective,
        'own': own,
        'inherited': [s for s in effective if s['inherited_from'] is not None],
    }


def _parse_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def set_own_specs(category, items):
    """Replace `category`'s OWN spec lines with `items` = [{definition, required}] in the wanted order.
    Every definition must be a live attribute of the category's shop. Rows dropped from the list are
    soft-deleted; a dropped-then-re-added attribute gets its old row back (unique per category)."""
    if not isinstance(items, list):
        raise ValidationError({'items': 'items must be a list.'})
    if len(items) > MAX_SPECS_PER_CATEGORY:
        raise ValidationError({'items': f'At most {MAX_SPECS_PER_CATEGORY} specs per category.'})
    wanted, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            raise ValidationError({'items': 'Each item must be an object.'})
        def_id = _parse_uuid(it.get('definition'))
        if def_id is None:
            raise ValidationError({'items': 'Each item needs a valid attribute id.'})
        if def_id in seen:
            raise ValidationError({'items': 'The same attribute is listed twice.'})
        seen.add(def_id)
        wanted.append((def_id, bool(it.get('required', False))))

    store = category.store
    defs = {d.id: d for d in AttributeDefinition.all_objects.filter(
        store=store, is_deleted=False, id__in=[w[0] for w in wanted])}
    if len(defs) != len(wanted):
        raise ValidationError({'items': 'Unknown attribute for this shop.'})

    with transaction.atomic():
        _lock_shop_specs(store)
        Category.all_objects.select_for_update().filter(pk=category.pk).first()
        existing = {r.definition_id: r for r in CategorySpec.all_objects.filter(category=category)}
        keep = set()
        for i, (def_id, required) in enumerate(wanted):
            row = existing.get(def_id)
            if row is None:
                row = CategorySpec(category=category, definition=defs[def_id])
            row.sort_order = i
            row.required = required
            row.is_deleted = False
            row.deleted_at = None
            row.save()
            keep.add(def_id)
        for def_id, row in existing.items():
            if def_id not in keep and not row.is_deleted:
                row.delete()   # soft
    return own_spec_rows(category)


def apply_preset(category, preset_key):
    """Attach a starter preset to `category`: each field's attribute is reused by `key` if the shop
    already has a live one (never modified), else created in the shop's language. Fields already in
    the category's effective spec set (own or inherited) are skipped — applying twice adds nothing.
    Returns {'created': [names of new attributes], 'attached': [names of new lines]}."""
    preset = SPEC_PRESETS.get(preset_key)
    if preset is None:
        raise ValidationError({'preset': 'Unknown preset.'})
    store = category.store
    lang = 'ar' if (store.default_language or '').lower().startswith('ar') else 'en'
    created, attached = [], []
    with transaction.atomic():
        _lock_shop_specs(store)
        Category.all_objects.select_for_update().filter(pk=category.pk).first()
        keys = [f['key'] for f in preset['fields']]
        by_key = {}
        for d in (AttributeDefinition.all_objects
                  .filter(store=store, is_deleted=False, key__in=keys).order_by('created_at')):
            by_key.setdefault(d.key, d)   # an old shop may hold two with one key — take the oldest

        effective_ids = {s['definition'] for s in effective_spec_set(category)}
        existing = {r.definition_id: r for r in CategorySpec.all_objects.filter(category=category)}
        next_order = max([r.sort_order for r in existing.values() if not r.is_deleted], default=-1) + 1

        for f in preset['fields']:
            d = by_key.get(f['key'])
            if d is None:
                d = AttributeDefinition.objects.create(
                    store=store, key=f['key'], name=f['name'][lang],
                    input_type=f['input_type'], options=list(f.get('options', [])),
                    unit=f.get('unit', ''))
                by_key[f['key']] = d
                created.append(d.name)
            if str(d.id) in effective_ids:
                continue
            row = existing.get(d.id)
            if row is None:
                row = CategorySpec(category=category, definition=d)
            row.sort_order = next_order
            row.required = bool(f.get('required'))
            row.is_deleted = False
            row.deleted_at = None
            row.save()
            next_order += 1
            effective_ids.add(str(d.id))
            attached.append(d.name)
    return {'created': created, 'attached': attached}


def spec_values_text(value, unit):
    """'16' + 'GB' → '16GB'; a value that already ends in its unit is left alone."""
    value = (value or '').strip()
    unit = (unit or '').strip()
    if not value:
        return ''
    if unit and not value.lower().endswith(unit.lower()):
        return f'{value}{unit}'
    return value


class SpecLineBuilder:
    """Builds the one-line spec summary for label stickers, caching each category's spec set so a
    2,000-product label job costs one query per distinct category, not per product."""

    def __init__(self):
        self._cache = {}

    def specs_for(self, category):
        if category is None:
            return []
        if category.id not in self._cache:
            self._cache[category.id] = effective_spec_set(category)
        return self._cache[category.id]

    def line(self, category, attributes):
        """`attributes` = the variant's ProductAttribute rows (prefetched)."""
        specs = self.specs_for(category)
        if not specs:
            return ''
        values = {str(a.definition_id): a.value for a in attributes}
        parts = [spec_values_text(values.get(s['definition']), s['unit']) for s in specs]
        return ' · '.join(p for p in parts if p)
