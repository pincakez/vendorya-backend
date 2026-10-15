"""Adapter: the Proline desktop ERP (MS SQL), exported table by table to CSV — part A, master data.

Column lists are Proline's own, copied from its schema (one CSV per table, header = column names,
UTF-8 with BOM). Everything Proline-specific lives in this file; the engine is `data_import/core.py`.

What lands where:
    tb_Group1 (top) + tb_Group2 (child, `Group1` → tb_Group1.ID or .Name)  → Category with parent
    tb_Cust                                   → Supplier and/or Customer (the split rule below)
    tb_Item                                   → Product + one ProductVariant
        ItemCode → SKU2 (and the hidden tag) · ItemName → name · PriceIn → cost · PriceOut1 → sell
        MinQty → reorder level · BarCode1 (else BarCode) → the one barcode · Group2ID/Group1ID → category
        SourceID → the supplier (a tb_Cust row) — else the shop's default supplier
    NOT imported, ever: stock (tb_Item.Qty, tb_StoreQty). Stock comes later as one correction.

The customer / supplier split — Proline has ONE table (tb_Cust) for both. Per row, first rule that applies:
    1. forced by --supplier-codes / --customer-codes
    2. a "type" column (Type1 / Type2 / ConType — auto-detected, or --type-column) whose value says
       supplier (مورد / supplier / vendor) or customer (عميل / customer / client)
    3. usage: the code appears in purchases (tb_InvoIn, tb_InvoInBack) or as an item's SourceID →
       supplier; in sales (tb_InvoOut) → customer; in both → both a supplier and a customer
    4. otherwise → customer
The report prints how many went each way and why, plus the values seen in the type columns.
"""
from collections import Counter

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from inventory.models import Category, Product, ProductVariant, Supplier
from users.models import Customer

from ..core import (Adapter, BadNumber, apply_fields, describe, has_value, norm_key, norm_name, norm_phone,
                    parse_decimal, read_table, text, truthy)
from ..models import ExternalRef

CREATED, LINKED = ExternalRef.Origin.CREATED, ExternalRef.Origin.LINKED

GROUP1_COLUMNS = ['ID', 'Name', 'BColor', 'BPic', 'BNStart', 'BNShow', 'Extra', 'NewPic', 'Amount', 'NameEn',
                  'web_name']
GROUP2_COLUMNS = ['ID', 'Group1', 'Name', 'BColor', 'BPic', 'BNStart', 'BNShow', 'NewPic', 'NameEn',
                  'WebItemStatus', 'web_name']
ITEM_COLUMNS = [
    'ItemCode', 'ItemName', 'ID', 'Group1ID', 'Group1Name', 'Group2ID', 'Group2Name', 'MadeIn', 'Memo', 'PriceIn',
    'PriceOut1', 'PriceOut2', 'PriceMini', 'MinQty', 'UnitQty', 'SourceID', 'SourceName', 'BarCode', 'Serial',
    'ExDate', 'Service', 'UserAdded', 'DateAdded', 'Qty', 'QtyAcc', 'QtyInOut', 'Row', 'Coll', 'Pic', 'BarCode1',
    'BarCode2', 'BarCode3', 'BarCode4', 'BarCode5', 'BarCode6', 'BarCode7', 'BarCode8', 'BarCode9', 'BColor', 'BPic',
    'BNStart', 'BNShow', 'Car', 'Kitchen', 'Discount', 'NewPic', 'Mobile', 'WebSite', 'Group0ID', 'Group0Name',
    'ItemNameEn', 'ItemNote', 'BranchCode', 'BranchName', 'Description', 'WebChkPrice', 'WebChkPriceD', 'WebChkDes',
    'WebChkSpc', 'WebShowQty', 'WebItemStatus', 'LastDateIn', 'LastDateOut', 'LastPriceIn', 'LastPriceOut',
    'ItemModel', 'ChkProfit_1', 'ChkProfit_2', 'Profit_1', 'Profit_2', 'Act', 'warranty', 'ColorGroup',
    'ColorDefault', 'Tags', 'Memo_ar', 'Description_ar', 'web_name', 'ItemNameInvoice', 'Rate', 'RateTotal',
    'UnitQty2', 'UnitQty2Val']
CUST_COLUMNS = [
    'ID', 'Name', 'Tel1', 'Tel2', 'Fax', 'Address', 'Name2', 'Mob', 'NID', 'Email', 'Web', 'Birthday', 'Price',
    'LimetAcc', 'BankNo', 'BankName', 'BankAccName', 'Tax1', 'Tax2', 'Tax3', 'Note', 'Type1', 'Type2', 'ConType',
    'UserAdded', 'DateAdded', 'FixDis', 'GUAName', 'GUAID', 'GUATel1', 'GUATel2', 'GUAWork', 'GUAAddress', 'Zone',
    'Username', 'Password', 'Debit', 'Credit', 'Balance', 'Title', 'country', 'city', 'building', 'floor', 'zip',
    'Address_2', 'Flat', 'AccountCode', 'FB_Id', 'FB_Photo']
INVO_IN_COLUMNS = [
    'InvoNo', 'Date', 'Time', 'InvoID', 'StoreCode', 'StoreName', 'ComCode', 'ComName', 'Note', 'TotalNoDis', 'Dis',
    'Total', 'Payed', 'Cre', 'InvoType', 'Cheeck', 'Visa', 'UserAdded', 'AllItem', 'AllItemQty', 'BranchCode',
    'BranchName', 'RefCode', 'RefName', 'discountRate']
INVO_IN_BACK_COLUMNS = INVO_IN_COLUMNS[:6] + ['PriceType'] + INVO_IN_COLUMNS[6:]
INVO_OUT_COLUMNS = [
    'InvoNo', 'Date', 'Time', 'InvoID', 'StoreCode', 'StoreName', 'PriceType', 'ComCode', 'ComName', 'Note',
    'TotalNoDis', 'Dis', 'Total', 'Payed', 'Cre', 'InvoType', 'Cheeck', 'Visa', 'ShowNo', 'UserAdded', 'UserPayID',
    'UserPayName', 'AllItem', 'TaxeS', 'TaxeG', 'Servc', 'Delev', 'InvoDelev', 'CurrencyPay', 'MoneyPay',
    'AllItemQty', 'InvoMark', 'InvoType2', 'BranchCode', 'BranchName', 'TableID', 'Personal', 'Booking', 'DateArr',
    'TimeArr', 'BookingStatus', 'EndRest', 'MiniMunPersonal', 'MinMunOnOff', 'Dayss', 'TableGroup', 'InvoNoDay',
    'MinimumTotal', 'Printed', 'MobPrinted', 'DownPayment', 'UserSerID', 'UserSerName', 'RefCode', 'RefName',
    'DiscountUnit', 'DiscountTotal', 'VoucherCode']

TABLES = {'tb_Group1': GROUP1_COLUMNS, 'tb_Group2': GROUP2_COLUMNS, 'tb_Cust': CUST_COLUMNS,
          'tb_Item': ITEM_COLUMNS, 'tb_InvoIn': INVO_IN_COLUMNS, 'tb_InvoInBack': INVO_IN_BACK_COLUMNS,
          'tb_InvoOut': INVO_OUT_COLUMNS}
PURCHASE_TABLES = ('tb_InvoIn', 'tb_InvoInBack')
SALES_TABLES = ('tb_InvoOut',)

TYPE_COLUMNS = ('Type1', 'Type2', 'ConType')
SUPPLIER_WORDS = ('مورد', 'supplier', 'vendor')
CUSTOMER_WORDS = ('عميل', 'عملاء', 'زبون', 'customer', 'client')
PHONE_COLUMNS = ('Mob', 'Tel1', 'Tel2')
# tb_Cust columns that land somewhere (the rest are counted as "dropped").
CUST_USED = {'ID', 'Name', 'Mob', 'Tel1', 'Tel2', 'Email', 'Note', 'LimetAcc', 'city', 'Balance', 'Debit', 'Credit'}
EXTRA_BARCODES = [f'BarCode{i}' for i in range(2, 10)]
SKU2_MAX = 8

BALANCE_HELP = (
    "tb_Cust has Debit, Credit and Balance side by side. Balance looks like a RUNNING total kept by Proline "
    "(Debit − Credit of every movement so far) — i.e. the CURRENT balance, not an opening one. Old sales, "
    "purchases and payments come in later as normal history (part B); importing Balance as an opening balance "
    "AND that history would count the same debt twice. So the default is --balances=none. Use "
    "--balances=opening ONLY if the history will NOT be imported (or the opening balance is cleared first).")


class ProlineAdapter(Adapter):
    system = 'proline'

    # ── load: read + count + blockers (no writes) ────────────────────────────────────────────
    def load(self):
        o, r = self.options, self.report
        for key, title in (('categories', 'Categories (tb_Group1 + tb_Group2)'),
                           ('suppliers', 'Suppliers (tb_Cust rows split as supplier)'),
                           ('customers', 'Customers (tb_Cust rows split as customer)'),
                           ('products', 'Products (tb_Item)')):
            r.section(key, title)          # the row sections print first, the facts after
        self.t = {}
        for name, cols in TABLES.items():
            self.t[name] = read_table(self.folder, name, cols)
            r.file(name, self.t[name])
        if not any(self.t[n] for n in ('tb_Group1', 'tb_Group2', 'tb_Cust', 'tb_Item')):
            r.blockers.append('No Proline master-data CSV (tb_Group1 / tb_Group2 / tb_Cust / tb_Item) in the folder.')
        self.balances = o.get('balances') or 'none'
        self.sign = -1 if o.get('balance_sign') == 'flip' else 1

        from inventory.sku import shop_code, sku2_active
        if not shop_code(self.store):
            r.blockers.append('The shop has no store code — a product SKU cannot be made.')
        if self.t['tb_Item'] and self.t['tb_Item'].rows and not sku2_active(self.store):
            r.blockers.append('SKU2 is not ON for this shop — Proline item codes go into SKU2. '
                              'Switch it on first (Admin → SYSTEM → SKU Management).')
        self._load_parties()
        self._load_items()
        self._check_default_supplier()
        self._load_balances()

    def _rows(self, name):
        t = self.t.get(name)
        return t.rows if t else []

    # ── parties: which tb_Cust row is a customer, a supplier, or both ───────────────────────
    def _load_parties(self):
        o, r = self.options, self.report
        info = r.section('split', 'Customers / suppliers split (tb_Cust)', info=True)
        rows = self._rows('tb_Cust')
        purchases, sales = set(), set()
        for name in PURCHASE_TABLES:
            purchases |= {norm_key(x.get('ComCode')) for x in self._rows(name)} - {''}
        sources = {norm_key(x.get('SourceID')) for x in self._rows('tb_Item')} - {''}
        for name in SALES_TABLES:
            sales |= {norm_key(x.get('ComCode')) for x in self._rows(name)} - {''}
        for name in PURCHASE_TABLES + SALES_TABLES:
            if self.t.get(name) is None:
                info.line(f'{name}.csv not found — the usage rule only sees the files that are there.')

        # Which tb_Cust column do invoices point at? ID unless AccountCode clearly matches more.
        used = purchases | sales | sources
        ids = {norm_key(x.get('ID')) for x in rows} - {''}
        accs = {norm_key(x.get('AccountCode')) for x in rows} - {''}
        hit_id, hit_acc = len(used & ids), len(used & accs)
        key_col = o.get('party_key') or ('AccountCode' if hit_acc > hit_id else 'ID')
        self.party_key = key_col
        info.line(f'tb_Cust key column: {key_col} (codes used in invoices/items: {len(used)} · found by ID '
                  f'{hit_id} · by AccountCode {hit_acc})')
        keys = ids if key_col == 'ID' else accs
        orphans = used - keys
        if orphans:
            info.line(f'{len(orphans)} codes used in invoices/items match no tb_Cust row '
                      f'(e.g. {", ".join(sorted(orphans)[:5])})')

        # The type column: given, none, or the one whose values name supplier/customer most often.
        tcol = o.get('type_column') or 'auto'
        cust_t = self.t.get('tb_Cust')
        for col in TYPE_COLUMNS:
            if cust_t is not None and cust_t.has(col):
                seen = Counter(text(x.get(col)) for x in rows if text(x.get(col)))
                if seen:
                    info.line(f'values in {col}: ' + ', '.join(f'"{v}" × {n}' for v, n in seen.most_common(6)))
        if tcol == 'auto':
            best, best_n = None, 0
            for col in TYPE_COLUMNS:
                n = sum(1 for x in rows if self._classify(x.get(col)))
                if n > best_n:
                    best, best_n = col, n
            tcol = best
            info.line(f'type column: {best} (auto — {best_n} rows name supplier/customer)' if best
                      else 'type column: none recognised (auto) — the split uses invoices only')
        elif tcol == 'none':
            tcol = None
            info.line('type column: off (--type-column none)')
        else:
            info.line(f'type column: {tcol} (--type-column)')
        self.type_col = tcol

        force_sup = {norm_key(c) for c in o.get('supplier_codes') or []}
        force_cus = {norm_key(c) for c in o.get('customer_codes') or []}
        self.parties, seen_keys = [], set()
        why = Counter()
        for x in rows:
            key, name = norm_key(x.get(key_col)), text(x.get('Name'))
            if key and key in force_sup and key in force_cus:
                kinds, reason = {'supplier', 'customer'}, 'forced by --supplier-codes and --customer-codes'
            elif key and key in force_sup:
                kinds, reason = {'supplier'}, 'forced by --supplier-codes'
            elif key and key in force_cus:
                kinds, reason = {'customer'}, 'forced by --customer-codes'
            elif tcol and self._classify(x.get(tcol)):
                kinds, reason = self._classify(x.get(tcol)), f'{tcol} = "{text(x.get(tcol))}"'
            elif key in purchases or key in sources or key in sales:
                kinds = set()
                if key in purchases or key in sources:
                    kinds.add('supplier')
                if key in sales:
                    kinds.add('customer')
                reason = {frozenset({'supplier'}): 'used in purchases or as an item source',
                          frozenset({'customer'}): 'used in sales',
                          frozenset({'supplier', 'customer'}): 'used in both purchases and sales'}[frozenset(kinds)]
            else:
                kinds, reason = {'customer'}, 'not used in any purchase → customer (default)'
            skip = None
            if not key:
                skip = f'empty {key_col}'
            elif key in seen_keys:
                skip = f'duplicate {key_col}'
            elif not name:
                skip = 'empty name'
            elif len(name) > 255:
                skip = 'name longer than 255 characters'
            seen_keys.add(key)
            if not skip:
                why[(' + '.join(sorted(kinds)), reason)] += 1
            self.parties.append({'key': key, 'row': x, 'kinds': kinds, 'skip': skip, 'name': name})
        for (kinds, reason), n in sorted(why.items(), key=lambda kv: -kv[1]):
            info.note(f'→ {kinds}: {reason}', n)

        # Fields with no home in Vendorya (counted, never added as fields in this step).
        if cust_t is not None:
            drop = r.section('dropped', 'tb_Cust fields with no place in Vendorya (rows that had a value)',
                             info=True)
            used_cols = CUST_USED | ({tcol} if tcol else set()) | {key_col}
            for col in CUST_COLUMNS:
                if col in used_cols or not cust_t.has(col):
                    continue
                n = sum(1 for x in rows if has_value(x.get(col)))
                if n:
                    drop.note(f'dropped: {col}', n)
            extra_phone = sum(1 for x in rows if len({norm_phone(x.get(c)) for c in PHONE_COLUMNS} - {''}) > 1)
            if extra_phone:
                drop.note('dropped: a 2nd / 3rd phone (Vendorya keeps one phone)', extra_phone)

        self.supplier_keys = {p['key'] for p in self.parties if not p['skip'] and 'supplier' in p['kinds']}

    def _check_default_supplier(self):
        """A NEW product needs a supplier (its SKU carries the supplier code): the item's SourceID supplier,
        else a default one. Items that end up linked to an existing product don't need it (not known yet)."""
        info = self.report.section('split')
        self.default_supplier = self._default_supplier()
        need = sum(1 for it in self.items
                   if not it['skip'] and norm_key(it['row'].get('SourceID')) not in self.supplier_keys)
        if need:
            if self.default_supplier is None:
                self.report.blockers.append(
                    f'Up to {need} items have no supplier in Proline (SourceID empty or not a supplier). '
                    f'Give --default-supplier <code> or set the shop\'s default supplier.')
            else:
                info.line(f'up to {need} items use the default supplier '
                          f'{self.default_supplier.name} ({self.default_supplier.code_prefix})')

    @staticmethod
    def _classify(raw):
        v = norm_name(raw)
        if not v:
            return set()
        kinds = set()
        if any(w in v for w in SUPPLIER_WORDS):
            kinds.add('supplier')
        if any(w in v for w in CUSTOMER_WORDS):
            kinds.add('customer')
        return kinds

    def _default_supplier(self):
        want = (self.options.get('default_supplier') or '').strip()
        live = Supplier.all_objects.filter(store=self.store, is_deleted=False)
        if want:
            sup = live.filter(code_prefix=want).first() or live.filter(name__iexact=want).first()
            if sup is None:
                self.report.blockers.append(f'--default-supplier "{want}": no such supplier in this shop.')
                return None
        else:
            sup = live.filter(pk=self.store.default_supplier_id).first() if self.store.default_supplier_id else None
        if sup is not None and not sup.prefix_locked:
            self.report.blockers.append(f'Default supplier {sup.name}: its code is not locked yet.')
            return None
        return sup

    # ── items: validate + count what has no home yet ────────────────────────────────────────
    def _load_items(self):
        rows = self._rows('tb_Item')
        info = self.report.section('item_extras', 'tb_Item data with no place in Vendorya yet (decides later work)',
                                   info=True)
        self.items, seen = [], set()
        for x in rows:
            code, name = text(x.get('ItemCode')), text(x.get('ItemName'))
            skip, prices = None, {}
            if not code:
                skip = 'empty ItemCode'
            elif code in seen:
                skip = 'duplicate ItemCode'
            elif not name:
                skip = 'empty name'
            elif len(name) > 255:
                skip = 'name longer than 255 characters'
            else:
                try:
                    for col in ('PriceIn', 'PriceOut1', 'MinQty'):
                        prices[col] = parse_decimal(x.get(col), col)
                except BadNumber as exc:
                    skip = f'bad number in {exc.column} ("{exc.raw}")'
                else:
                    if any(v is not None and v < 0 for v in prices.values()):
                        skip = 'negative price or MinQty'
            if code:
                seen.add(code)
            sku2_ok = code.isdigit() and len(code) <= SKU2_MAX
            self.items.append({'code': code, 'name': name, 'row': x, 'skip': skip, 'prices': prices,
                               'sku2_ok': sku2_ok})
            if code and not skip and not sku2_ok:
                info.note(f'item code cannot be SKU2 ({"longer than 8" if code.isdigit() else "not only digits"}) '
                          f'— imported without SKU2, the tag keeps the code')

        def count(label, pred):
            n = sum(1 for x in rows if pred(x))
            if n:
                info.note(label, n)

        def dec(x, col):
            try:
                return parse_decimal(x.get(col), col) or 0
            except BadNumber:
                return 0

        count('rows with ANY extra barcode (BarCode2..BarCode9)', lambda x: any(has_value(x.get(c)) for c in EXTRA_BARCODES))
        for c in EXTRA_BARCODES:
            count(f'  {c} set', lambda x, c=c: has_value(x.get(c)))
        count('BarCode (no number) set and different from BarCode1',
              lambda x: has_value(x.get('BarCode')) and text(x.get('BarCode')) != text(x.get('BarCode1')))
        count('PriceOut2 set (≠ 0)', lambda x: dec(x, 'PriceOut2') != 0)
        count('PriceOut2 set and different from PriceOut1', lambda x: dec(x, 'PriceOut2') not in (0, dec(x, 'PriceOut1')))
        count('PriceMini set (≠ 0)', lambda x: dec(x, 'PriceMini') != 0)
        count('Serial flag on (serial per unit)', lambda x: truthy(x.get('Serial')))
        count('Act off (inactive in Proline — imported anyway)', lambda x: has_value(x.get('Act')) is False and text(x.get('Act')) != '')
        count('Qty ≠ 0 (stock — NOT imported; one correction on switch-over day)', lambda x: dec(x, 'Qty') != 0)
        count('Group0ID set (a level above Group1 — not imported)', lambda x: has_value(x.get('Group0ID')))
        count('UnitQty2 set (a second unit)', lambda x: has_value(x.get('UnitQty2')))
        count('warranty > 0', lambda x: dec(x, 'warranty') > 0)
        count('ItemNameEn set', lambda x: has_value(x.get('ItemNameEn')))
        count('Description / Memo set', lambda x: has_value(x.get('Description')) or has_value(x.get('Memo')))

    # ── balances: facts only (the decision is the --balances flag) ──────────────────────────
    def _load_balances(self):
        info = self.report.section('balances', 'Balances (tb_Cust.Balance)', info=True)
        nonzero = total = checked = dc = cd = bad = 0
        kinds = Counter()
        for p in self.parties:
            x = p['row']
            try:
                b, d, c = (parse_decimal(x.get(k), k) for k in ('Balance', 'Debit', 'Credit'))
            except BadNumber:
                bad += 1
                continue
            if b:
                nonzero += 1
                total += b
                if not p['skip']:
                    kinds[' + '.join(sorted(p['kinds']))] += 1
            if b is not None and d is not None and c is not None:
                checked += 1
                dc += b == d - c
                cd += b == c - d
        info.line(f'--balances={self.balances}' + (' (balance sign flipped)' if self.sign < 0 else ''))
        info.line(f'{nonzero} rows have a Balance ≠ 0 · total {total}')
        if checked:
            info.line(f'Balance = Debit − Credit on {dc} of {checked} rows · = Credit − Debit on {cd} '
                      f'(all rows ⇒ Balance is a running total = the CURRENT balance)')
        if bad:
            info.line(f'{bad} rows with a bad number in Balance / Debit / Credit')
        for k, n in kinds.items():
            info.note(f'non-zero balance on a {k}', n)
        if self.balances == 'none':
            info.line('NOT imported. ' + BALANCE_HELP)
        else:
            info.line('Imported as the customers\' OPENING balance. Suppliers have no opening-balance field in '
                      'Vendorya — their balances are not imported. A party that is both is not imported either.')
            self.report.warnings.append('--balances=opening: ' + BALANCE_HELP)

    # ── run: the writes ──────────────────────────────────────────────────────────────────────
    def run(self):
        self._categories()
        self._suppliers()
        self._customers()
        self._products()

    # categories
    def _categories(self):
        sec = self.report.section('categories', 'Categories (tb_Group1 + tb_Group2)')
        tagged = self.refs.tagged_ids('category')
        self._free_cats = {}
        for c in Category.all_objects.filter(store=self.store, is_deleted=False):
            if c.id not in tagged:
                self._free_cats.setdefault((c.parent_id, norm_name(c.name)), c)
        self.cat_g1, self.cat_g1_name, self.cat_g2 = {}, {}, {}
        seen = set()
        for x in self._rows('tb_Group1'):
            key, name = norm_key(x.get('ID')), text(x.get('Name'))
            sample = f'G1 {key} "{name}"'
            if not self._cat_row_ok(sec, key, name, seen, sample):
                continue
            cat = self._upsert_category(sec, f'g1:{key}', name, None, sample)
            if cat is not None:
                self.cat_g1[key] = cat
                self.cat_g1_name.setdefault(norm_name(name), cat)
        seen = set()
        for x in self._rows('tb_Group2'):
            key, name, up = norm_key(x.get('ID')), text(x.get('Name')), norm_key(x.get('Group1'))
            sample = f'G2 {key} "{name}"'
            if not self._cat_row_ok(sec, key, name, seen, sample):
                continue
            parent = self.cat_g1.get(up)
            if parent is not None:
                sec.note('Group2 → Group1 found by ID')
            else:
                parent = self.cat_g1_name.get(norm_name(x.get('Group1')))
                if parent is None:
                    sec.skip('parent group (Group1) not found', sample)
                    continue
                sec.note('Group2 → Group1 found by name')
            cat = self._upsert_category(sec, f'g2:{key}', name, parent, sample)
            if cat is not None:
                self.cat_g2[key] = cat

    @staticmethod
    def _cat_row_ok(sec, key, name, seen, sample):
        if not key:
            sec.skip('empty ID', sample)
            return False
        if key in seen:
            sec.skip('duplicate ID', sample)
            return False
        seen.add(key)
        if not name:
            sec.skip('empty name', sample)
            return False
        if len(name) > 150:
            sec.skip('name longer than 150 characters', sample)
            return False
        return True

    def _upsert_category(self, sec, ext, name, parent, sample):
        with self.row(sec, sample):
            ref, obj = self.refs.target('category', ext, Category)
            if ref is not None:
                if obj is None or obj.is_deleted:
                    sec.skip('deleted in Vendorya — left alone', sample)
                    return None
                self.refs.touch(ref)
                changes = apply_fields(obj, {'name': name, 'parent': parent}, ref.origin)
                if changes:
                    obj.save()
                    sec.update(f'{sample}: {describe(changes)}')
                else:
                    sec.unchanged()
                return obj
            obj = self._free_cats.pop((parent.pk if parent else None, norm_name(name)), None)
            if obj is not None:
                self.refs.tag('category', ext, obj, LINKED)
                sec.link(f'{sample} → existing "{obj.name}"')
                return obj
            obj = Category(store=self.store, name=name, parent=parent)
            obj.save()
            self.refs.tag('category', ext, obj, CREATED)
            sec.add(sample + (f' under "{parent.name}"' if parent else ''))
            return obj
        return None

    # parties: shared bits
    def _party_values(self, sec, x, phone_max):
        phones = [norm_phone(x.get(c)) for c in PHONE_COLUMNS]
        phone = next((p for p in phones if p), '')
        if len(phone) > phone_max:
            sec.note(f'phone longer than {phone_max} characters — not imported')
            phone = ''
        email = text(x.get('Email'))
        if email:
            try:
                validate_email(email)
            except ValidationError:
                sec.note('email not valid — not imported')
                email = ''
        city = text(x.get('city'))
        if len(city) > 100:
            sec.note('city longer than 100 characters — not imported')
            city = ''
        return {'name': text(x.get('Name')), 'phone': phone, 'email': email, 'notes': text(x.get('Note')),
                'city': city}

    # suppliers
    def _suppliers(self):
        from inventory.sku import next_free_supplier_code
        sec = self.report.section('suppliers', 'Suppliers (tb_Cust rows split as supplier)')
        tagged = self.refs.tagged_ids('supplier')
        free = {}
        for s in Supplier.all_objects.filter(store=self.store, is_deleted=False):
            if s.id not in tagged:
                free.setdefault(norm_name(s.name), s)
        self.sup_by_key = {}
        for p in self.parties:
            if 'supplier' not in p['kinds']:
                continue
            sample = f'{p["key"]} "{p["name"]}"'
            if p['skip']:
                sec.skip(p['skip'], sample)
                continue
            v = self._party_values(sec, p['row'], 20)
            values = {'name': v['name'], 'phone_number': v['phone'], 'email': v['email'], 'notes': v['notes'],
                      'city': v['city']}
            with self.row(sec, sample):
                ref, obj = self.refs.target('supplier', p['key'], Supplier)
                if ref is not None:
                    if obj is None or obj.is_deleted:
                        sec.skip('deleted in Vendorya — left alone', sample)
                        continue
                    self.refs.touch(ref)
                    changes = apply_fields(obj, values, ref.origin)
                    if changes:
                        obj.save()
                        sec.update(f'{sample}: {describe(changes)}')
                    else:
                        sec.unchanged()
                else:
                    obj = free.pop(norm_name(v['name']), None)
                    if obj is not None:
                        changes = apply_fields(obj, values, LINKED)
                        if changes:
                            obj.save()
                        self.refs.tag('supplier', p['key'], obj, LINKED)
                        sec.link(f'{sample} → existing supplier {obj.code_prefix}'
                                 + (f' ({describe(changes)})' if changes else ''))
                    else:
                        code = next_free_supplier_code(self.store)
                        if code is None:
                            sec.skip('no free supplier code left', sample)
                            continue
                        obj = Supplier(store=self.store, code_prefix=code, prefix_locked=True, **values)
                        obj.save()
                        self.refs.tag('supplier', p['key'], obj, CREATED)
                        sec.add(f'{sample} → code {code}')
                self.sup_by_key[p['key']] = obj

    # customers
    def _customers(self):
        sec = self.report.section('customers', 'Customers (tb_Cust rows split as customer)')
        # Only a customer that existed BEFORE this run and carries no tag may be linked — two Proline rows
        # are never merged into one customer.
        tagged = self.refs.tagged_ids('customer')
        self._linkable = {c for c in Customer.all_objects.filter(store=self.store, is_deleted=False,
                                                                 is_walk_in=False).values_list('id', flat=True)
                          if c not in tagged}
        # The phone is unique per shop across ALL customer rows (the DB rule ignores soft delete).
        everyone = list(Customer.all_objects.filter(store=self.store))
        self._phone_owner = {c.phone_number: c for c in everyone if c.phone_number}
        set_balances = 0
        for p in self.parties:
            if 'customer' not in p['kinds']:
                continue
            sample = f'{p["key"]} "{p["name"]}"'
            if p['skip']:
                sec.skip(p['skip'], sample)
                continue
            x = p['row']
            v = self._party_values(sec, x, 20)
            values = {'name': v['name'], 'email': v['email'], 'notes': v['notes'] or None, 'city': v['city']}
            try:
                limit = parse_decimal(x.get('LimetAcc'), 'LimetAcc')
            except BadNumber:
                sec.note('bad number in LimetAcc — credit limit left empty')
                limit = None
            # Empty / 0 → no own limit (= the shop's default). A customer the shop typed by hand keeps its own.
            values['credit_limit'] = limit if limit is not None and limit > 0 else None
            if values['credit_limit'] is None and not has_value(x.get('LimetAcc')):
                sec.note('LimetAcc empty or 0 → the shop\'s default credit limit')
            always = ()
            if self.balances == 'opening':
                if p['kinds'] == {'customer'}:
                    try:
                        bal = parse_decimal(x.get('Balance'), 'Balance')
                    except BadNumber:
                        bal = None
                        sec.note('bad number in Balance — opening balance not set')
                    if bal is not None:
                        values['balance'] = bal * self.sign
                        always = ('balance',)
                elif has_value(x.get('Balance')):
                    sec.note('balance of a party that is customer AND supplier — not imported')
            with self.row(sec, sample):
                ref, obj = self.refs.target('customer', p['key'], Customer)
                if ref is not None:
                    if obj is None or obj.is_deleted:
                        sec.skip('deleted in Vendorya — left alone', sample)
                        continue
                    self.refs.touch(ref)
                    origin, verb = ref.origin, 'update'
                else:
                    obj, origin, verb = self._match_customer(v), LINKED, 'link'
                    if obj is None:
                        obj, origin, verb = Customer(store=self.store), CREATED, 'add'
                values['phone_number'] = self._free_phone(sec, v['phone'], obj)
                if obj.pk and obj.phone_number and not values['phone_number']:
                    values.pop('phone_number')     # never blank a phone the row already has
                old_balance = obj.balance
                changes = apply_fields(obj, values, origin, always)
                if verb == 'add':
                    obj.save()
                    self.refs.tag('customer', p['key'], obj, CREATED)
                    sec.add(sample)
                elif verb == 'link':
                    if changes:
                        obj.save()
                    self.refs.tag('customer', p['key'], obj, LINKED)
                    sec.link(f'{sample} → existing customer "{obj.name}"'
                             + (f' ({describe(changes)})' if changes else ''))
                elif changes:
                    obj.save()
                    sec.update(f'{sample}: {describe(changes)}')
                else:
                    sec.unchanged()
                if obj.phone_number:
                    self._phone_owner[obj.phone_number] = obj
                if 'balance' in values and obj.balance != old_balance:
                    set_balances += 1
        if self.balances == 'opening':
            sec.line(f'opening balance set / changed on {set_balances} customers')

    def _match_customer(self, v):
        """A customer the shop typed by hand before the import = same phone AND same name. A phone alone
        is not enough (one phone is often shared by a family)."""
        other = self._phone_owner.get(v['phone']) if v['phone'] else None
        if other is not None and other.id in self._linkable and norm_name(other.name) == norm_name(v['name']):
            self._linkable.discard(other.id)
            return other
        return None

    def _free_phone(self, sec, phone, obj):
        if not phone:
            return None
        owner = self._phone_owner.get(phone)
        if owner is not None and owner.pk != obj.pk:
            sec.note('phone already used by another customer — imported without it')
            return None
        return phone

    # products
    def _products(self):
        from inventory.code_search import barcode_owner
        from search.indexing import sync_products_on_commit
        sec = self.report.section('products', 'Products (tb_Item)')
        by_sku2 = {v.sku2: v for v in ProductVariant.all_objects.filter(store=self.store, sku2__isnull=False)
                   .select_related('product')}
        # A product typed by hand may carry a source code as its BARCODE instead (e.g. a 2nd unit of the same
        # model had its own code in the source) — that code must link to it too, never make a 2nd product.
        by_barcode = {}
        for v in (ProductVariant.all_objects.filter(store=self.store, is_deleted=False, product__is_deleted=False,
                                                    product__source=Product.Source.STORE, barcode__isnull=False)
                  .select_related('product')):
            by_barcode.setdefault(v.barcode.strip(), v)
        created = self.refs.tagged_ids('product', origin=CREATED)
        handled = {}      # variant id → the code that set its prices in this run (one product, several codes)
        touched = set()
        # Order: codes that are already a product's SKU2 first (they own that product's prices), then codes
        # that fit SKU2 (a shop that auto-numbers SKU2 must not hand out a source code before its own item
        # arrives), then the rest. Stable, so the same files always give the same result.
        first = set(by_sku2)
        for it in sorted(self.items, key=lambda i: (i['code'] not in first, not i['sku2_ok'])):
            code, x = it['code'], it['row']
            sample = f'{code} "{it["name"]}"'
            if it['skip']:
                sec.skip(it['skip'], sample)
                continue
            pr = it['prices']
            category = (self.cat_g2.get(norm_key(x.get('Group2ID'))) or self.cat_g1.get(norm_key(x.get('Group1ID'))))
            if category is None and (has_value(x.get('Group1ID')) or has_value(x.get('Group2ID'))):
                sec.note('group not imported / not found — product has no category')
            barcode = text(x.get('BarCode1')) or text(x.get('BarCode'))
            if barcode and barcode == code:
                sec.note('barcode = item code — not stored twice (SKU2 already scans)')
                barcode = ''
            if len(barcode) > 100:
                sec.note('barcode longer than 100 characters — not imported')
                barcode = ''
            prod_vals = {'name': it['name'], 'category': category}
            var_vals = {'cost_price': pr['PriceIn'] or 0, 'sell_price': pr['PriceOut1'] or 0,
                        'reorder_level': pr['MinQty'] or 0}
            with self.row(sec, sample):
                ref, v = self.refs.target('product', code, ProductVariant)
                origin, verb = None, None
                if ref is not None:
                    if v is None or v.is_deleted or v.product.is_deleted:
                        sec.skip('deleted in Vendorya — left alone', sample)
                        continue
                    self.refs.touch(ref)
                    origin, verb = ref.origin, 'update'
                else:
                    cand = by_sku2.get(code) if it['sku2_ok'] else None
                    if cand is not None and cand.id in created:
                        sec.skip('SKU2 already belongs to another imported item', sample)
                        continue
                    if cand is not None and (cand.is_deleted or cand.product.is_deleted
                                             or cand.product.source != Product.Source.STORE):
                        sec.note('SKU2 held by a deleted product — imported without SKU2')
                        cand = None
                    if cand is None and by_barcode.get(code) is not None and by_barcode[code].id not in created:
                        cand = by_barcode[code]
                        sec.note('item code is the BARCODE of a product typed by hand — linked to that product')
                    if cand is not None:
                        v, origin, verb = cand, LINKED, 'link'
                if verb is None:
                    supplier = self.sup_by_key.get(norm_key(x.get('SourceID'))) or self.default_supplier
                    if supplier is None:
                        sec.skip('no supplier (SourceID not a supplier, no default supplier)', sample)
                        continue
                    product = Product(store=self.store, name=it['name'], category=category, supplier=supplier)
                    product.save()
                    v = ProductVariant(product=product, **var_vals)
                    if it['sku2_ok'] and code not in by_sku2:
                        v.sku2 = code
                    if barcode and not barcode_owner(self.store, barcode):
                        v.barcode = barcode
                    elif barcode:
                        sec.note('barcode already used by another product — not imported')
                    v.save()
                    if v.sku2:
                        by_sku2[v.sku2] = v
                    self.refs.tag('product', code, v, CREATED)
                    touched.add(product.pk)
                    sec.add(f'{sample} → SKU {v.sku}' + (f' · SKU2 {v.sku2}' if v.sku2 else '')
                            + (f' · {category.name}' if category else ''))
                    continue
                product = v.product
                always = ('cost_price', 'sell_price', 'reorder_level')
                if v.id in handled:
                    # one Vendorya product for several source items: the first code keeps its prices
                    sec.note('several source items share one Vendorya product — prices from the first code')
                    for f in always:
                        var_vals.pop(f)
                    prod_vals = {}
                handled.setdefault(v.id, code)
                p_changes = apply_fields(product, prod_vals, origin)
                if origin == LINKED and norm_name(product.name) != norm_name(it['name']):
                    sec.note('name differs from Proline — kept the name typed in Vendorya')
                if barcode and barcode != (v.barcode or ''):
                    if barcode_owner(self.store, barcode, skip_variant_ids=[v.id]):
                        sec.note('barcode already used by another product — not imported')
                    else:
                        var_vals['barcode'] = barcode
                if it['sku2_ok'] and not v.sku2 and code not in by_sku2:
                    var_vals['sku2'] = code
                v_changes = apply_fields(v, var_vals, origin, always)
                sup = self.sup_by_key.get(norm_key(x.get('SourceID')))
                if sup is not None and sup.pk != product.supplier_id:
                    sec.note('supplier differs from Proline — kept (the SKU carries the supplier code)')
                if p_changes:
                    product.save(update_fields=[f for f, _, _ in p_changes] + ['updated_at'])
                if v_changes:
                    v.save(update_fields=[f for f, _, _ in v_changes] + ['updated_at'])
                    if v.sku2:
                        by_sku2[v.sku2] = v
                changes = p_changes + v_changes
                if changes:
                    touched.add(product.pk)
                if verb == 'link':
                    self.refs.tag('product', code, v, LINKED)
                    sec.link(f'{sample} → existing SKU {v.sku}' + (f' ({describe(changes)})' if changes else ''))
                elif changes:
                    sec.update(f'{sample}: {describe(changes)}')
                else:
                    sec.unchanged()
        if touched:
            sync_products_on_commit(touched)   # a variant-only save fires no search signal
