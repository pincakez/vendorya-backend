"""Catalog CSV import / export.

Header convention (agreed with the product owner):
  * PREFIX-first  = a fixed Vendorya concept (mandatory structure)
      M_BRANCH, A_SUPP, M_CAT, S1_CAT, S2_CAT, S3_CAT, Q_QTY, W_PRICE, R_PRICE, P_NAME, P_REF
  * SUFFIX-last   = a user-defined attribute, suffix = input type
      <NAME>_DD (dropdown/SELECT), <NAME>_FT (free text/TEXT), <NAME>_NO (number)

Rules:
  * CSV only. SKU is never imported (auto-generated from the supplier prefix).
  * The file can NEVER create a supplier or a branch — they must already exist.
  * Categories are capped at 4 tiers (M_CAT + up to S1/S2/S3).
  * Validation is all-or-nothing: any error rejects the whole file.
  * Product name = P_NAME, else composed from BRAND + MODEL.
  * Duplicate (same name under the same supplier) is rejected.
  * M_CAT must be a column, but its value may be empty = a product with no category (s166).
  * P_REF (optional): rows with the same P_REF are the VARIANTS of one product — they must share
    A_SUPP, P_NAME and the category path; each row keeps its own prices, stock, attributes, SKU2 (s166).
    Without P_REF every row is its own product (and a repeated name is still rejected).
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
    # SKU: read-only reference in the export — accepted and IGNORED on import (s159: before this an
    # exported file could not be imported back). SKU2: the product's second code (shop's SKU2 must be ON).
    'SKU', 'SKU2',
    'P_REF',   # s166: groups rows into one multi-variant product
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

    @staticmethod
    def _group_key(row, i):
        """Rows sharing a non-empty P_REF become one product; any other row is its own product."""
        ref = (row.get('P_REF') or '').strip()
        return ('ref', ref.lower()) if ref else ('row', i)

    def _row_name(self, headers, row):
        if 'P_NAME' in headers and (row.get('P_NAME') or '').strip():
            return (row['P_NAME']).strip()
        brand = (row.get('BRAND_DD') or '').strip()
        model = (row.get('MODEL_FT') or '').strip()
        composed = ' '.join(p for p in (brand, model) if p).strip()
        return composed

    @staticmethod
    def _attr_values(attr_headers, row):
        return tuple((row.get(h) or '').strip() for h in attr_headers)

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
        from inventory.sku import sku2_active
        sku2_on = sku2_active(self.store)
        sku2_taken = set(ProductVariant.all_objects.filter(store=self.store, sku2__isnull=False)
                         .values_list('sku2', flat=True))
        sku2_in_file = set()

        seen_in_file = set()
        groups = {}                        # group key -> first row's {row, name, supp, path, attrs}
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

            # category chain (an empty M_CAT = no category, s166)
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

            # name + duplicate (checked once per product — the first row of a P_REF group)
            name = self._row_name(headers, row)
            gkey = self._group_key(row, i)
            first = groups.get(gkey)
            if first is not None:
                ref = row['P_REF'].strip()
                if (first['name'].lower(), first['supp'].lower(), first['path']) != (name.lower(), supp_name.lower(), path):
                    err(f'P_REF "{ref}" must keep the same A_SUPP, P_NAME and categories as row {first["row"]}.')
                if first['attrs'] == self._attr_values(attr_headers, row) and attr_headers:
                    warnings.append(f'Row {i}: same attributes as row {first["row"]} (P_REF "{ref}") — '
                                    f'two variants that look identical.')
            else:
                groups[gkey] = {'row': i, 'name': name, 'supp': supp_name, 'path': path,
                                'attrs': self._attr_values(attr_headers, row)}
            if not name:
                err('cannot determine a product name (add P_NAME or BRAND/MODEL).')
            elif supplier is not None and first is None:
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

            # SKU2 (s159)
            code2 = (row.get('SKU2') or '').strip()
            if code2:
                if not sku2_on:
                    err('SKU2 is not switched on for this shop (ask the platform admin).')
                elif not code2.isdigit() or len(code2) > 8:
                    err(f'SKU2 "{code2}" must be 1 to 8 digits.')
                elif code2 in sku2_taken:
                    err(f'SKU2 "{code2}" already belongs to another product.')
                elif code2 in sku2_in_file:
                    err(f'SKU2 "{code2}" appears twice in the file.')
                else:
                    sku2_in_file.add(code2)

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
            'products': len(groups),
            'variants': len(rows),
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
        products = {}       # group key -> Product (P_REF rows share one)
        for i, row in enumerate(rows, start=2):
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

            gkey = self._group_key(row, i)
            product = products.get(gkey)
            if product is None:
                product = products[gkey] = Product.objects.create(
                    store=self.store, name=self._row_name(headers, row),
                    category=category, supplier=supplier,
                )
                created += 1
            variant = ProductVariant(
                product=product,
                cost_price=_parse_decimal(row.get('W_PRICE')) or Decimal('0'),
                sell_price=_parse_decimal(row.get('R_PRICE')) or Decimal('0'),
                sku2=(row.get('SKU2') or '').strip() or None,
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
    ignores it). P_NAME carries the product name (s165, §SKU2): without it the
    importer could only rebuild a name from BRAND_DD + MODEL_FT, so an exported
    file failed to import back for any product without those attributes."""
    products = (Product.objects.filter(store=store)
                .select_related('category', 'category__parent',
                                'category__parent__parent',
                                'category__parent__parent__parent', 'supplier')
                .prefetch_related('variants__attributes__definition',
                                  'variants__stock_levels__branch'))

    defs = list(AttributeDefinition.objects.filter(store=store).order_by('name'))
    attr_headers = [f'{d.key.upper()}_{_SUFFIX_FOR_TYPE[d.input_type]}' for d in defs]
    def_by_id = {d.id: f'{d.key.upper()}_{_SUFFIX_FOR_TYPE[d.input_type]}' for d in defs}

    rows, max_depth, multi = [], 1, False
    for ref, p in enumerate(products, start=1):
        path = _cat_path(p.category) if p.category else []
        max_depth = max(max_depth, len(path))
        variants = list(p.variants.all())
        multi = multi or len(variants) > 1
        for v in variants:
            avals = {def_by_id.get(a.definition_id): a.value for a in v.attributes.all()}
            levels = v.stock_levels.all()
            qty = sum((sl.quantity for sl in levels), Decimal('0'))
            branch = next((sl.branch.name for sl in levels if sl.quantity), None) or 'Main'
            rows.append({
                'M_BRANCH': branch,
                'A_SUPP': p.supplier.name if p.supplier else '',
                'P_NAME': p.name,
                'P_REF': ref,
                '_path': path,
                '_attrs': avals,
                'Q_QTY': qty,
                'W_PRICE': v.cost_price,
                'R_PRICE': v.sell_price,
                'SKU': v.sku,
                'SKU2': v.sku2 or '',
            })

    cat_headers = CAT_COLUMNS[:max_depth]
    # SKU2 column once the shop has ever had SKU2 (ON, or DISABLED with history kept).
    with_sku2 = store.settings.sku2_state != 'OFF'
    # P_REF only when some product has 2+ variants — it is what lets those rows import back as ONE product (s166).
    ref_cols = ['P_REF'] if multi else []
    header = (['M_BRANCH', 'A_SUPP', 'P_NAME'] + ref_cols + cat_headers + attr_headers + ['Q_QTY', 'W_PRICE', 'R_PRICE', 'SKU']
              + (['SKU2'] if with_sku2 else []))

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for r in rows:
        line = [r['M_BRANCH'], r['A_SUPP'], r['P_NAME']] + ([r['P_REF']] if multi else [])
        for idx in range(len(cat_headers)):
            line.append(r['_path'][idx] if idx < len(r['_path']) else '')
        for h in attr_headers:
            line.append(r['_attrs'].get(h, ''))
        line += [r['Q_QTY'], r['W_PRICE'], r['R_PRICE'], r['SKU']] + ([r['SKU2']] if with_sku2 else [])
        writer.writerow(line)
    return out.getvalue()
