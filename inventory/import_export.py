"""Catalog CSV import / export.

Header convention (agreed with the product owner):
  * PREFIX-first  = a fixed Vendorya concept (mandatory structure)
      M_BRANCH, A_SUPP, M_CAT, S1_CAT, S2_CAT, S3_CAT, Q_QTY, W_PRICE, R_PRICE, P_NAME, P_REF
  * SUFFIX-last   = a user-defined attribute, suffix = input type
      <NAME>_DD (dropdown/SELECT), <NAME>_FT (free text/TEXT), <NAME>_NO (number)

Rules:
  * CSV only. A NEW product's SKU is auto-generated from the supplier prefix (leave SKU empty).
  * Re-import UPDATES (s168, Yakot 2026-10-14 "prices + name, never stock"): a row whose SKU is a live
    product of THIS shop updates that variant's W_PRICE / R_PRICE and the product's P_NAME (an empty
    P_NAME keeps the name) — nothing else: stock (Q_QTY), supplier, branch, categories, attributes and
    SKU2 stay as they are (a stock difference is reported as a warning). A SKU this shop does not have
    (a typo, or another shop's file) is an ERROR, never a silent new product — to copy a catalog into
    another shop, empty or delete the SKU column first.
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
    # SKU: the variant's SKU (in every export). On import a row WITH a SKU updates that product of this
    # shop (s168); an empty SKU = a new product. SKU2: the product's second code (shop's SKU2 must be ON).
    'SKU', 'SKU2',
    'P_REF',   # s166: groups rows into one multi-variant product
}
REQUIRED_COLUMNS = ['A_SUPP', 'M_CAT', 'W_PRICE', 'R_PRICE']
CENTS = Decimal('0.01')
MAX_CHANGES_LISTED = 200     # the row-by-row "what will change" list is cut here (the counts never are)

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

    # ---- SKU matching (s168: a re-import UPDATES) -------------------------
    def _live_variants_by_sku(self, lock=False):
        """This shop's live STORE variants, by exact SKU and by SKU with the dashes taken out (someone
        may type 1234400100 for 1234-400-100 — the dash-less form counts only when it is unambiguous).
        Another shop's SKU is never in here: a SKU is unique per shop, so it can mean nothing else."""
        qs = (ProductVariant.all_objects
              .filter(product__store=self.store, is_deleted=False, product__is_deleted=False,
                      product__source=Product.Source.STORE, sku__isnull=False)
              .select_related('product', 'product__supplier')
              .prefetch_related('stock_levels'))
        if lock:
            qs = qs.select_for_update(of=('self',))
        exact, flat = {}, {}
        for v in qs:
            exact[v.sku] = v
            flat.setdefault(v.sku.replace('-', ''), []).append(v)
        return exact, {k: vs[0] for k, vs in flat.items() if len(vs) == 1}

    # ---- full validation (no writes) ------------------------------------
    def validate(self, headers, rows, _lock=False):
        """Check the whole file. A row whose SKU is one of this shop's products is an UPDATE row (only
        W_PRICE, R_PRICE and P_NAME are applied); a row with an empty SKU is a NEW product. The planned
        updates are kept on `self._plan` / `self._renames` for commit()."""
        errors = self._validate_headers(headers)
        warnings = []
        self._plan, self._renames = {}, {}
        if errors:
            return {'ok': False, 'errors': errors, 'warnings': [], 'summary': {}}

        if not rows:
            return {'ok': False, 'errors': ['The file has no data rows.'],
                    'warnings': [], 'summary': {}}

        suppliers = {s.name.lower(): s for s in Supplier.objects.filter(store=self.store)}
        branches = {b.name.lower(): b for b in Branch.objects.filter(store=self.store)}
        existing = {}                      # (name.lower, supplier_id) -> product ids already in catalog
        for p in Product.objects.filter(store=self.store).only('id', 'name', 'supplier_id'):
            existing.setdefault((p.name.lower(), p.supplier_id), set()).add(p.id)
        attr_headers = [h for h in headers if _is_attr(h)]
        from inventory.sku import sku2_active
        sku2_on = sku2_active(self.store)
        sku2_taken = set(ProductVariant.all_objects.filter(store=self.store, sku2__isnull=False)
                         .values_list('sku2', flat=True))
        sku2_in_file = set()
        by_sku, by_flat = self._live_variants_by_sku(lock=_lock)

        seen_in_file = set()
        groups = {}                        # NEW rows: group key -> first row's {row, name, supp, path, attrs}
        new_rows = 0
        new_cat_paths, new_attr_options = set(), {}
        ref_groups = {}                    # P_REF -> {'row', 'update': bool, 'products': set}
        sku_rows = {}                      # variant id -> first row that named it
        upd_products = {}                  # product id -> {'row', 'name' (from P_NAME, '' = keep), 'product'}
        stock_ignored = sku2_ignored = not_found = 0

        for i, row in enumerate(rows, start=2):   # row 1 = header
            def err(msg):
                errors.append(f'Row {i}: {msg}')

            code = (row.get('SKU') or '').strip()
            is_update = bool(code)
            variant = None
            if is_update:
                variant = by_sku.get(code) or by_flat.get(code.replace('-', ''))
                if variant is None:
                    not_found += 1
                    err(f'SKU "{code}" not found in this shop — leave SKU empty to add the row as a new product.')
                elif variant.id in sku_rows:
                    err(f'SKU "{code}" appears twice in the file (row {sku_rows[variant.id]}).')
                    variant = None
                else:
                    sku_rows[variant.id] = i

            gkey = self._group_key(row, i)
            ref = (row.get('P_REF') or '').strip()
            if ref:
                g = ref_groups.setdefault(gkey, {'row': i, 'update': is_update, 'products': set()})
                if g['update'] != is_update:
                    err(f'P_REF "{ref}" mixes rows with and without SKU (row {g["row"]}) — '
                        f'a new variant can\'t be added to an existing product by import.')
                elif variant is not None:
                    if g['products'] and variant.product_id not in g['products']:
                        err(f'P_REF "{ref}" holds SKUs of different products (see row {g["row"]}).')
                    g['products'].add(variant.product_id)

            if is_update:
                # Only the name and the prices can change; supplier, branch, categories, attributes,
                # SKU2 and stock of an existing product are left exactly as they are.
                new_name = (row.get('P_NAME') or '').strip() if 'P_NAME' in headers else ''
                if variant is not None:
                    prod = variant.product
                    seen = upd_products.get(prod.id)
                    if seen is None:
                        upd_products[prod.id] = {'row': i, 'name': new_name, 'product': prod}
                    elif new_name and seen['name'] and new_name != seen['name']:
                        err(f'same product as row {seen["row"]} but a different P_NAME '
                            f'("{seen["name"]}" vs "{new_name}").')
                    elif new_name and not seen['name']:
                        seen['name'] = new_name
                    code2 = (row.get('SKU2') or '').strip()
                    if 'SKU2' in headers and code2 != (variant.sku2 or ''):
                        sku2_ignored += 1
            else:
                new_rows += 1
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
                first = groups.get(gkey)
                if first is not None:
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
                        err(f'"{name}" already exists for supplier "{supp_name}" '
                            f'(to update it, put its SKU in the SKU column).')
                    elif key in seen_in_file:
                        err(f'"{name}" is duplicated in the file for supplier "{supp_name}".')
                    else:
                        seen_in_file.add(key)

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

                # collect dropdown options for the summary
                for h in attr_headers:
                    _, itype = _attr_meta(h)
                    v = (row.get(h) or '').strip()
                    if itype == AttributeDefinition.InputType.SELECT and v:
                        new_attr_options.setdefault(h, set()).add(v)

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
            q = None
            if 'Q_QTY' in headers:
                try:
                    q = _parse_decimal(row.get('Q_QTY'))
                    if q is not None and q < 0:
                        err('Q_QTY cannot be negative.')
                except InvalidOperation:
                    err(f'Q_QTY "{row.get("Q_QTY")}" is not a number.')

            # the planned price update of an existing variant
            if variant is not None and w is not None and r is not None and w >= 0 and r >= 0:
                try:
                    cost, sell = w.quantize(CENTS), r.quantize(CENTS)
                except InvalidOperation:
                    err('price is too large.')
                else:
                    self._plan[i] = {
                        'variant': variant,
                        'cost': cost if cost != variant.cost_price else None,
                        'sell': sell if sell != variant.sell_price else None,
                    }
                if q is not None:
                    on_hand = sum((sl.quantity for sl in variant.stock_levels.all()), Decimal('0'))
                    if q != on_hand:
                        stock_ignored += 1

        # renames: the new name must not collide with another product of the same supplier
        renamed_keys = {}
        for pid, u in upd_products.items():
            prod, name = u['product'], u['name']
            if not name or name == prod.name:
                continue
            key = (name.lower(), prod.supplier_id)
            if existing.get(key, set()) - {pid} or key in seen_in_file:
                errors.append(f'Row {u["row"]}: can\'t rename to "{name}" — another product of the same '
                              f'supplier already has that name.')
            elif key in renamed_keys:
                errors.append(f'Row {u["row"]}: "{name}" is also the new name of row {renamed_keys[key]}.')
            else:
                renamed_keys[key] = u['row']
                self._renames[pid] = (prod, name)

        if not_found > 1 and not sku_rows:
            errors.insert(0, 'None of the SKUs in this file belong to this shop — is it an export from another '
                             'shop? To add these rows as new products, empty or delete the SKU column.')
        if errors:
            self._plan, self._renames = {}, {}
            return {'ok': False, 'errors': errors, 'warnings': warnings, 'summary': {}}

        if stock_ignored:
            warnings.append(f'Stock (Q_QTY) is ignored for {stock_ignored} existing product row(s) whose quantity '
                            f'differs from the system — use a stock adjustment to change stock.')
        if sku2_ignored:
            warnings.append(f'SKU2 is not changed for existing products ({sku2_ignored} row(s) differ) — '
                            f'edit it on the product.')

        # what will change, row by row (shown before the import + kept in the activity log)
        updated = unchanged = price_changes = 0
        changes = []
        for i, plan in self._plan.items():
            v = plan['variant']
            rename = self._renames.get(v.product_id)
            show_rename = rename is not None and upd_products[v.product_id]['row'] == i
            priced = plan['cost'] is not None or plan['sell'] is not None
            if priced:
                price_changes += 1
            if not (priced or rename):
                unchanged += 1
                continue
            updated += 1
            change = {'row': i, 'sku': v.sku, 'name': v.product.name}
            if show_rename:
                change['new_name'] = rename[1]
            if plan['cost'] is not None:
                change['cost'] = [str(v.cost_price), str(plan['cost'])]
            if plan['sell'] is not None:
                change['sell'] = [str(v.sell_price), str(plan['sell'])]
            changes.append(change)

        summary = {
            'products': len(groups),          # NEW products
            'variants': new_rows,             # NEW variant rows
            'category_paths': len(new_cat_paths),
            'attributes': len(attr_headers) if new_rows else 0,
            'attribute_columns': attr_headers,
            'updated': updated,               # existing variant rows with a new price and/or product name
            'unchanged': unchanged,
            'price_changes': price_changes,   # rows whose cost and/or sell price changes
            'name_changes': len(self._renames),   # products renamed
            'stock_ignored': stock_ignored,
            'changes': changes[:MAX_CHANGES_LISTED],
            'changes_more': max(0, len(changes) - MAX_CHANGES_LISTED),
        }
        return {'ok': True, 'errors': [], 'warnings': warnings, 'summary': summary}

    # ---- commit (writes, atomic) ----------------------------------------
    @transaction.atomic
    def commit(self, headers, rows):
        result = self.validate(headers, rows, _lock=True)
        if not result['ok']:
            return result

        # 1. updates of existing products: prices per variant, name per product — nothing else.
        touched = set()
        for plan in self._plan.values():
            v, fields = plan['variant'], []
            if plan['cost'] is not None:
                v.cost_price = plan['cost']; fields.append('cost_price')
            if plan['sell'] is not None:
                v.sell_price = plan['sell']; fields.append('sell_price')
            if fields:
                v.save(update_fields=fields + ['updated_at'])
                touched.add(v.product_id)
        for pid, (prod, name) in self._renames.items():
            prod.name = name
            prod.save(update_fields=['name', 'updated_at'])   # post_save re-indexes the name
            touched.add(pid)
        if touched:
            # a variant price save fires no search signal — bring those products' docs up to date
            from search.indexing import sync_products_on_commit
            sync_products_on_commit(touched)

        # 2. new products (rows with an empty SKU) — exactly as before s168
        new_rows = [(i, row) for i, row in enumerate(rows, start=2) if not (row.get('SKU') or '').strip()]
        suppliers = {s.name.lower(): s for s in Supplier.objects.filter(store=self.store)}
        branches = {b.name.lower(): b for b in Branch.objects.filter(store=self.store)}
        attr_headers = [h for h in headers if _is_attr(h)] if new_rows else []
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
        for i, row in new_rows:
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
    One row per variant. SKU is included so the file can be edited and imported
    back as an UPDATE of prices + names (s168). P_NAME carries the product name (s165, §SKU2): without it the
    importer could only rebuild a name from BRAND_DD + MODEL_FT, so an exported
    file failed to import back for any product without those attributes."""
    # STORE products only (s168): a Memory Base entry has no SKU and no supplier, so its row could never
    # import back — it would block the whole re-import.
    products = (Product.objects.filter(store=store, source=Product.Source.STORE)
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
