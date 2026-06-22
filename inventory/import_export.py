"""Catalog CSV import / export.

Header convention (agreed with the product owner):
  * PREFIX-first  = a fixed Vendorya concept (mandatory structure)
      M_BRANCH, A_SUPP, M_CAT, S1_CAT, S2_CAT, S3_CAT, Q_QTY, W_PRICE, R_PRICE, P_NAME
  * SUFFIX-last   = a user-defined attribute, suffix = input type
      <NAME>_DD (dropdown/SELECT), <NAME>_FT (free text/TEXT), <NAME>_NO (number)

Rules:
  * CSV only. SKU is never imported (auto-generated from the supplier prefix).
  * The file can NEVER create a supplier or a branch — they must already exist.
  * Categories are capped at 4 tiers (M_CAT + up to S1/S2/S3).
  * Validation is all-or-nothing: any error rejects the whole file.
  * Product name = P_NAME, else composed from BRAND + MODEL.
  * Duplicate (same name under the same supplier) is rejected.
"""
import csv
import io
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.text import slugify

from core.models import Branch
from .models import (
    Category, Supplier, Product, ProductVariant, ProductAttribute,
    AttributeDefinition, StockAdjustment, MAX_CATEGORY_DEPTH,
)

# Mandatory (prefix-first) columns we understand.
CAT_COLUMNS = ['M_CAT', 'S1_CAT', 'S2_CAT', 'S3_CAT']          # tiers 1..4
MANDATORY_COLUMNS = set(CAT_COLUMNS) | {
    'M_BRANCH', 'A_SUPP', 'Q_QTY', 'W_PRICE', 'R_PRICE', 'P_NAME',
}
REQUIRED_COLUMNS = ['A_SUPP', 'M_CAT', 'W_PRICE', 'R_PRICE']

# Attribute suffix -> AttributeDefinition.InputType
ATTR_SUFFIX = {
    'DD': AttributeDefinition.InputType.SELECT,
    'FT': AttributeDefinition.InputType.TEXT,
    'NO': AttributeDefinition.InputType.NUMBER,
}


def _is_attr(header):
    parts = header.rsplit('_', 1)
    return len(parts) == 2 and parts[1] in ATTR_SUFFIX and len(parts[0]) > 0


def _attr_meta(header):
    name, suffix = header.rsplit('_', 1)
    return name, ATTR_SUFFIX[suffix]


def _parse_decimal(raw):
    """'13,600.00' -> Decimal('13600.00'); '' -> None; junk -> ValueError."""
    s = (raw or '').strip().replace(',', '').replace(' ', '')
    if s == '':
        return None
    return Decimal(s)   # raises InvalidOperation on junk


def parse_csv(file_bytes):
    """Bytes -> (headers, rows[list[dict]]). Rows keyed by header."""
    text = file_bytes.decode('utf-8-sig', errors='replace')
    reader = csv.reader(io.StringIO(text))
    all_rows = [r for r in reader]
    if not all_rows:
        return [], []
    headers = [h.strip() for h in all_rows[0]]
    rows = []
    for raw in all_rows[1:]:
        if not any((c or '').strip() for c in raw):
            continue   # skip blank lines
        rows.append({headers[i]: (raw[i] if i < len(raw) else '') for i in range(len(headers))})
    return headers, rows


class CatalogImporter:
    def __init__(self, store, user):
        self.store = store
        self.user = user

    # ---- header analysis -------------------------------------------------
    def _validate_headers(self, headers):
        errors = []
        seen = set()
        for h in headers:
            if h in seen:
                errors.append(f'Duplicate column "{h}".')
            seen.add(h)
            if h in MANDATORY_COLUMNS or _is_attr(h):
                continue
            # anything else is unknown — including category tiers past the cap
            if h.endswith('_CAT'):
                errors.append(f'Column "{h}" exceeds the {MAX_CATEGORY_DEPTH}-tier category limit.')
            else:
                errors.append(f'Unknown column "{h}".')

        for req in REQUIRED_COLUMNS:
            if req not in headers:
                errors.append(f'Missing required column "{req}".')

        # category tiers must be contiguous (no gaps)
        for i in range(1, len(CAT_COLUMNS)):
            if CAT_COLUMNS[i] in headers and CAT_COLUMNS[i - 1] not in headers:
                errors.append(
                    f'Column "{CAT_COLUMNS[i]}" needs "{CAT_COLUMNS[i - 1]}" to exist first.')
        return errors

    def _row_name(self, headers, row):
        if 'P_NAME' in headers and (row.get('P_NAME') or '').strip():
            return (row['P_NAME']).strip()
        brand = (row.get('BRAND_DD') or '').strip()
        model = (row.get('MODEL_FT') or '').strip()
        composed = ' '.join(p for p in (brand, model) if p).strip()
        return composed

    # ---- full validation (no writes) ------------------------------------
    def validate(self, headers, rows):
        errors = self._validate_headers(headers)
        warnings = []
        if errors:
            return {'ok': False, 'errors': errors, 'warnings': [], 'summary': {}}

        if not rows:
            return {'ok': False, 'errors': ['The file has no data rows.'],
                    'warnings': [], 'summary': {}}

        suppliers = {s.name.lower(): s for s in Supplier.objects.filter(store=self.store)}
        branches = {b.name.lower(): b for b in Branch.objects.filter(store=self.store)}
        existing = {                       # (name.lower, supplier_id) already in catalog
            (p.name.lower(), p.supplier_id)
            for p in Product.objects.filter(store=self.store).only('name', 'supplier_id')
        }
        attr_headers = [h for h in headers if _is_attr(h)]

        seen_in_file = set()
        new_cat_paths, new_attr_options = set(), {}

        for i, row in enumerate(rows, start=2):   # row 1 = header
            def err(msg):
                errors.append(f'Row {i}: {msg}')

            # supplier
            supp_name = (row.get('A_SUPP') or '').strip()
            supplier = suppliers.get(supp_name.lower())
            if not supp_name:
                err('A_SUPP is empty.')
            elif supplier is None:
                err(f'supplier "{supp_name}" does not exist (the file can\'t create suppliers).')
            elif not supplier.prefix_locked:
                err(f'supplier "{supp_name}" has no locked SKU prefix yet.')

            # branch
            br_name = (row.get('M_BRANCH') or '').strip() or 'Main'
            if br_name.lower() not in branches:
                err(f'branch "{br_name}" does not exist.')

            # category chain
            if not (row.get('M_CAT') or '').strip():
                err('M_CAT is empty.')
            path = []
            broken = False
            for col in CAT_COLUMNS:
                if col not in headers:
                    break
                val = (row.get(col) or '').strip()
                if val:
                    if broken:
                        err(f'{col} is set but a higher tier is empty.')
                        break
                    path.append(val)
                else:
                    broken = True
            if path:
                new_cat_paths.add(tuple(path))

            # name + duplicate
            name = self._row_name(headers, row)
            if not name:
                err('cannot determine a product name (add P_NAME or BRAND/MODEL).')
            elif supplier is not None:
                key = (name.lower(), supplier.id)
                if key in existing:
                    err(f'"{name}" already exists for supplier "{supp_name}".')
                elif key in seen_in_file:
                    err(f'"{name}" is duplicated in the file for supplier "{supp_name}".')
                else:
                    seen_in_file.add(key)

            # prices
            try:
                w = _parse_decimal(row.get('W_PRICE'))
                if w is None or w < 0:
                    err('W_PRICE must be a number ≥ 0.')
            except InvalidOperation:
                err(f'W_PRICE "{row.get("W_PRICE")}" is not a number.'); w = None
            try:
                r = _parse_decimal(row.get('R_PRICE'))
                if r is None or r < 0:
                    err('R_PRICE must be a number ≥ 0.')
            except InvalidOperation:
                err(f'R_PRICE "{row.get("R_PRICE")}" is not a number.'); r = None
            if w is not None and r is not None and r < w:
                warnings.append(f'Row {i}: retail ({r}) is below wholesale ({w}) — negative margin.')

            # quantity
            if 'Q_QTY' in headers:
                try:
                    q = _parse_decimal(row.get('Q_QTY'))
                    if q is not None and q < 0:
                        err('Q_QTY cannot be negative.')
                except InvalidOperation:
                    err(f'Q_QTY "{row.get("Q_QTY")}" is not a number.')

            # collect dropdown options for the summary
            for h in attr_headers:
                _, itype = _attr_meta(h)
                v = (row.get(h) or '').strip()
                if itype == AttributeDefinition.InputType.SELECT and v:
                    new_attr_options.setdefault(h, set()).add(v)

        if errors:
            return {'ok': False, 'errors': errors, 'warnings': warnings, 'summary': {}}

        summary = {
            'products': len(rows),
            'category_paths': len(new_cat_paths),
            'attributes': len(attr_headers),
            'attribute_columns': attr_headers,
        }
        return {'ok': True, 'errors': [], 'warnings': warnings, 'summary': summary}

    # ---- commit (writes, atomic) ----------------------------------------
    @transaction.atomic
    def commit(self, headers, rows):
        result = self.validate(headers, rows)
        if not result['ok']:
            return result

        suppliers = {s.name.lower(): s for s in Supplier.objects.filter(store=self.store)}
        branches = {b.name.lower(): b for b in Branch.objects.filter(store=self.store)}
        attr_headers = [h for h in headers if _is_attr(h)]
        attr_defs = {}      # header -> AttributeDefinition

        # resolve / create attribute definitions up front
        for h in attr_headers:
            token, itype = _attr_meta(h)
            key = slugify(token)
            defn, _ = AttributeDefinition.objects.get_or_create(
                store=self.store, key=key,
                defaults={'name': token.replace('_', ' ').title(), 'input_type': itype, 'options': []},
            )
            attr_defs[h] = defn

        created = 0
        for row in rows:
            supplier = suppliers[(row['A_SUPP']).strip().lower()]
            branch = branches[((row.get('M_BRANCH') or '').strip() or 'Main').lower()]

            # category chain (get_or_create down the path)
            parent = None
            for col in CAT_COLUMNS:
                if col not in headers:
                    break
                val = (row.get(col) or '').strip()
                if not val:
                    break
                parent, _ = Category.objects.get_or_create(
                    store=self.store, name=val, parent=parent)
            category = parent

            product = Product.objects.create(
                store=self.store, name=self._row_name(headers, row),
                category=category, supplier=supplier,
            )
            variant = ProductVariant(
                product=product,
                cost_price=_parse_decimal(row.get('W_PRICE')) or Decimal('0'),
                sell_price=_parse_decimal(row.get('R_PRICE')) or Decimal('0'),
            )
            variant.save()   # auto-generates SKU from the locked supplier prefix

            for h in attr_headers:
                v = (row.get(h) or '').strip()
                if not v:
                    continue
                defn = attr_defs[h]
                if defn.input_type == AttributeDefinition.InputType.SELECT and v not in defn.options:
                    defn.options.append(v)
                    defn.save(update_fields=['options'])
                ProductAttribute.objects.create(variant=variant, definition=defn, value=v)

            qty = _parse_decimal(row.get('Q_QTY')) if 'Q_QTY' in headers else None
            if qty and qty > 0:
                StockAdjustment.objects.create(
                    store=self.store, branch=branch, variant=variant,
                    quantity_change=qty, reason=StockAdjustment.Reason.OPENING,
                    notes='Imported opening stock', adjusted_by=self.user,
                )
            created += 1

        result['summary']['created'] = created
        return result


# ---- export -------------------------------------------------------------
_SUFFIX_FOR_TYPE = {
    AttributeDefinition.InputType.SELECT: 'DD',
    AttributeDefinition.InputType.TEXT:   'FT',
    AttributeDefinition.InputType.NUMBER: 'NO',
}


def _cat_path(category):
    names, node, guard = [], category, 0
    while node is not None and guard < 10:
        names.append(node.name)
        node = node.parent
        guard += 1
    names.reverse()
    return names


def export_catalog(store):
    """Dump the store's catalog to a CSV string in the same import schema.
    One row per variant. SKU is included as a read-only reference (the importer
    ignores it)."""
    products = (Product.objects.filter(store=store)
                .select_related('category', 'category__parent',
                                'category__parent__parent',
                                'category__parent__parent__parent', 'supplier')
                .prefetch_related('variants__attributes__definition',
                                  'variants__stock_levels__branch'))

    defs = list(AttributeDefinition.objects.filter(store=store).order_by('name'))
    attr_headers = [f'{d.key.upper()}_{_SUFFIX_FOR_TYPE[d.input_type]}' for d in defs]
    def_by_id = {d.id: f'{d.key.upper()}_{_SUFFIX_FOR_TYPE[d.input_type]}' for d in defs}

    rows, max_depth = [], 1
    for p in products:
        path = _cat_path(p.category) if p.category else []
        max_depth = max(max_depth, len(path))
        for v in p.variants.all():
            avals = {def_by_id.get(a.definition_id): a.value for a in v.attributes.all()}
            levels = v.stock_levels.all()
            qty = sum((sl.quantity for sl in levels), Decimal('0'))
            branch = next((sl.branch.name for sl in levels if sl.quantity), None) or 'Main'
            rows.append({
                'M_BRANCH': branch,
                'A_SUPP': p.supplier.name if p.supplier else '',
                '_path': path,
                '_attrs': avals,
                'Q_QTY': qty,
                'W_PRICE': v.cost_price,
                'R_PRICE': v.sell_price,
                'SKU': v.sku,
            })

    cat_headers = CAT_COLUMNS[:max_depth]
    header = ['M_BRANCH', 'A_SUPP'] + cat_headers + attr_headers + ['Q_QTY', 'W_PRICE', 'R_PRICE', 'SKU']

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for r in rows:
        line = [r['M_BRANCH'], r['A_SUPP']]
        for idx in range(len(cat_headers)):
            line.append(r['_path'][idx] if idx < len(r['_path']) else '')
        for h in attr_headers:
            line.append(r['_attrs'].get(h, ''))
        line += [r['Q_QTY'], r['W_PRICE'], r['R_PRICE'], r['SKU']]
        writer.writerow(line)
    return out.getvalue()
