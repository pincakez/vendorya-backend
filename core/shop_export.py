"""Owner "export my shop's data" (s167, Yakot 2026-10-14).

GET /api/core/shop-export/  →  ONE zip of CSV files holding this shop's own data:
products, customers, suppliers, staff (username / name / role only), sales + sales lines, sales payments,
customer receipts, purchases + purchase lines, supplier payments, refunds + refund lines, expenses — plus a
README.txt that lists every file and its columns.

Rules this module keeps:
- OWNER only (`role_map`), the platform admin is refused by RoleScopedPermission (§PRIVACY-SUDO).
- Every query is filtered by the caller's shop; soft-deleted rows are LEFT OUT (and so are the lines and
  payments of a soft-deleted invoice / refund / purchase).
- No secrets: no password hashes, tokens, API keys, PINs or user settings — staff = username, name, role.
- Memory-safe: the zip is written into a SpooledTemporaryFile (RAM up to a few MB, then a temp file on disk);
  each CSV is written row by row from `.iterator()` querysets, never a giant list. (The products file reuses
  the Import/Export page's `export_catalog()`, which builds its CSV in memory — fine for a catalog, not for
  100k sales lines, which is why the big tables do not go through it.)
- Each CSV = UTF-8 with a BOM (Excel opens Arabic right), Western digits, ISO dates in the shop's timezone,
  money as plain decimals (`1234.50`), a header row, ids + numbers so lines join to their invoices.
- Money figures reuse the ONE formulas: a customer's balance = `users.views.with_live_balance()` (the bulk
  twin of `customer_outstanding()`), what a sale still owes = `finance.models.invoice_outstanding()`.
"""
import csv
import io
import re
import tempfile
import zipfile
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from django.http import FileResponse
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import SimpleRateThrottle

from users.permissions import RoleScopedPermission

from .activity import log_activity
from .models import ActivityLog

SPOOL_MAX_BYTES = 4 * 1024 * 1024   # above this the zip moves from RAM to a temp file on disk
CHUNK = 2000                        # rows fetched per round-trip by .iterator()


# ── cell formatting ──────────────────────────────────────────────────────────
_NUMBER_LIKE = re.compile(r'[+\-]?[\d\s().\-]*')


def _safe_text(s):
    """Stop a text cell from running as a spreadsheet formula (CSV injection): a value starting with
    = @ TAB CR, or a + / - that is not just a phone/number, gets a leading apostrophe."""
    if not s:
        return s
    first = s[0]
    if first in '=@\t\r':
        return "'" + s
    if first in '+-' and not _NUMBER_LIKE.fullmatch(s):
        return "'" + s
    return s


def _cell(value, tz):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = value.astimezone(tz)
        return value.isoformat(timespec='seconds')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return _safe_text(str(value))


def _username(user):
    return user.username if user else ''


def _sku(variant):
    return variant.sku if variant else ''


def _product_name(variant):
    return variant.product.name if variant and variant.product_id else ''


# ── the tables ───────────────────────────────────────────────────────────────
# Each table = (file name, one-line description, [(column, description)], rows(store) generator).
# The README is generated from these same lists, so it can never drift from the files.

def _customers(store):
    from users.models import Customer
    from users.views import with_live_balance
    qs = with_live_balance(Customer.objects.filter(store=store)).order_by('created_at')
    for c in qs.iterator(chunk_size=CHUNK):
        yield [c.id, c.name, c.phone_number, c.whatsapp_number, c.email, c.gender, c.country, c.city,
               c.instagram, c.website, c.notes, c.is_walk_in, c.balance, c.live_balance, c.credit_limit,
               c.store_credit, c.created_at]


def _suppliers(store):
    from inventory.models import Supplier
    for s in Supplier.objects.filter(store=store).order_by('created_at').iterator(chunk_size=CHUNK):
        yield [s.id, s.name, s.company_name, s.code_prefix, s.phone_number, s.whatsapp_number, s.email,
               s.instagram, s.website, s.country, s.city, s.contact_info, s.notes, s.created_at]


def _staff(store):
    from users.models import User
    qs = User.objects.filter(store=store, is_superadmin=False).order_by('username')
    for u in qs.only('username', 'first_name', 'last_name', 'role').iterator(chunk_size=CHUNK):
        yield [u.username, u.first_name, u.last_name, u.role]


def _sales(store):
    from finance.models import SalesInvoice, invoice_outstanding, with_refund_totals
    qs = with_refund_totals(SalesInvoice.objects.filter(store=store)
                            .select_related('branch', 'customer')).order_by('date', 'invoice_number')
    for inv in qs.iterator(chunk_size=CHUNK):
        owed = invoice_outstanding(inv) if inv.status == SalesInvoice.Status.POSTED else None
        yield [inv.id, inv.invoice_number, inv.date, inv.status, inv.branch.name if inv.branch_id else '',
               inv.customer_id, inv.customer.name if inv.customer_id else '', inv.subtotal, inv.discount,
               inv.tax_total, inv.grand_total, inv.paid_amount, inv.change_given, inv.refunded, owed]


def _sales_lines(store):
    from finance.models import SalesInvoiceItem
    qs = (SalesInvoiceItem.objects
          .filter(invoice__store=store, invoice__is_deleted=False)
          .select_related('invoice', 'variant__product', 'unit')
          .order_by('invoice__date', 'invoice__invoice_number', 'id'))
    for it in qs.iterator(chunk_size=CHUNK):
        yield [it.id, it.invoice_id, it.invoice.invoice_number, _sku(it.variant), _product_name(it.variant),
               it.unit.name if it.unit_id else '', it.unit_factor, it.quantity, it.unit_price,
               it.discount_amount, it.tax_name, it.tax_rate, it.taxable_amount, it.tax_amount, it.total,
               it.cost_at_sale]


def _sales_payments(store):
    from finance.models import Payment
    qs = (Payment.objects
          .filter(invoice__store=store, invoice__is_deleted=False)
          .select_related('invoice', 'method', 'created_by')
          .order_by('created_at'))
    for p in qs.iterator(chunk_size=CHUNK):
        yield [p.id, p.created_at, p.invoice_id, p.invoice.invoice_number, p.method.name if p.method_id else '',
               p.amount, p.receipt_id, _username(p.created_by)]


def _customer_receipts(store):
    from finance.models import CustomerReceipt
    qs = (CustomerReceipt.objects.filter(store=store)
          .select_related('customer', 'method', 'created_by').order_by('date'))
    for r in qs.iterator(chunk_size=CHUNK):
        yield [r.id, r.date, r.customer_id, r.customer.name if r.customer_id else '',
               r.method.name if r.method_id else '', r.amount, r.unallocated, r.advance, r.note,
               _username(r.created_by)]


def _purchases(store):
    from finance.models import PurchaseInvoice
    qs = (PurchaseInvoice.objects.filter(store=store)
          .select_related('branch', 'supplier').order_by('date'))
    for p in qs.iterator(chunk_size=CHUNK):
        yield [p.id, p.purchase_number, p.vendor_reference, p.date, p.status,
               p.branch.name if p.branch_id else '', p.supplier_id, p.supplier.name if p.supplier_id else '',
               p.total_amount, p.paid_amount, p.notes]


def _purchase_lines(store):
    from finance.models import PurchaseItem
    qs = (PurchaseItem.objects
          .filter(invoice__store=store, invoice__is_deleted=False)
          .select_related('invoice', 'variant__product', 'unit')
          .order_by('invoice__date', 'id'))
    for it in qs.iterator(chunk_size=CHUNK):
        yield [it.id, it.invoice_id, it.invoice.purchase_number, _sku(it.variant), _product_name(it.variant),
               it.unit.name if it.unit_id else '', it.unit_factor, it.quantity, it.unit_cost, it.total_cost,
               it.expiry_date, it.batch_number]


def _supplier_payments(store):
    from finance.models import SupplierPayment
    qs = (SupplierPayment.objects.filter(store=store)
          .select_related('supplier', 'payment_method', 'created_by').order_by('date'))
    for p in qs.iterator(chunk_size=CHUNK):
        # The shop's own payment method (PROLINE-PARITY Upgrade 6); the old fixed code only when none was mapped.
        method = p.payment_method.name if p.payment_method_id else p.method
        yield [p.id, p.date, p.supplier_id, p.supplier.name if p.supplier_id else '', method, p.amount,
               p.note, _username(p.created_by)]


def _refunds(store):
    from finance.models import RefundInvoice
    qs = (RefundInvoice.objects.filter(store=store)
          .select_related('branch', 'original_invoice', 'customer', 'created_by').order_by('date'))
    for r in qs.iterator(chunk_size=CHUNK):
        orig = r.original_invoice
        yield [r.id, r.refund_number, r.date, r.branch.name if r.branch_id else '', r.original_invoice_id,
               orig.invoice_number if orig else '', r.customer_id, r.customer.name if r.customer_id else '',
               r.total_refunded, r.restocking_fee, r.paid_back, r.store_credit_back, r.drawer_cash,
               r.refund_method, r.reason, _username(r.created_by)]


def _refund_lines(store):
    from finance.models import RefundItem
    qs = (RefundItem.objects
          .filter(refund__store=store, refund__is_deleted=False)
          .select_related('refund', 'variant__product')
          .order_by('refund__date', 'id'))
    for it in qs.iterator(chunk_size=CHUNK):
        yield [it.id, it.refund_id, it.refund.refund_number, _sku(it.variant), _product_name(it.variant),
               it.unit_factor, it.quantity, it.refund_amount, it.restock_inventory]


def _expenses(store):
    from finance.models import Expense
    qs = Expense.objects.filter(store=store).select_related('branch', 'category').order_by('date', 'created_at')
    for e in qs.iterator(chunk_size=CHUNK):
        yield [e.id, e.date, e.branch.name if e.branch_id else '', e.category.name if e.category_id else '',
               e.amount, e.description]


TABLES = [
    ('customers.csv', 'Your customers.', [
        ('customer_id', 'Unique id (join key for sales, refunds, receipts)'),
        ('name', ''), ('phone', ''), ('whatsapp', ''), ('email', ''), ('gender', ''), ('country', ''),
        ('city', ''), ('instagram', ''), ('website', ''), ('notes', ''),
        ('is_walk_in', 'true for the shared Walk-in customer'),
        ('opening_balance', 'The starting balance typed in when the customer was added'),
        ('balance', 'What the customer owes you today (negative = you owe them), same number as the Customers page'),
        ('credit_limit', 'Empty = the shop default'), ('store_credit', 'Credit the customer can spend'),
        ('created_at', ''),
    ], _customers),
    ('suppliers.csv', 'Your suppliers.', [
        ('supplier_id', 'Unique id (join key for purchases and supplier payments)'),
        ('name', ''), ('company_name', ''), ('code_prefix', 'The supplier part of your SKUs'), ('phone', ''),
        ('whatsapp', ''), ('email', ''), ('instagram', ''), ('website', ''), ('country', ''), ('city', ''),
        ('contact_info', ''), ('notes', ''), ('created_at', ''),
    ], _suppliers),
    ('staff.csv', 'The people who can sign in to your shop (no passwords or other secrets).', [
        ('username', ''), ('first_name', ''), ('last_name', ''), ('role', 'OWNER / ADMIN / MANAGER / CASHIER'),
    ], _staff),
    ('sales.csv', 'Sales invoices (one row per invoice).', [
        ('invoice_id', 'Unique id (join key for sales_lines and sales_payments)'),
        ('invoice_number', 'The number printed on the receipt'), ('date', ''),
        ('status', 'DRAFT / POSTED / VOID'), ('branch', ''), ('customer_id', ''), ('customer_name', ''),
        ('subtotal', ''), ('discount', ''), ('tax_total', ''), ('grand_total', ''),
        ('paid_amount', 'Money received on this invoice'), ('change_given', 'Change handed back in cash'),
        ('returned', 'Value of goods returned from this invoice'),
        ('still_owed', 'What the customer still owes on this invoice (posted invoices only)'),
    ], _sales),
    ('sales_lines.csv', 'The items on each sales invoice.', [
        ('line_id', ''), ('invoice_id', 'Joins to sales.csv'), ('invoice_number', ''), ('sku', ''),
        ('product_name', ''), ('unit', 'The selling unit (e.g. pack), empty = base unit'),
        ('unit_factor', 'How many base units one selling unit holds'), ('quantity', 'In the selling unit'),
        ('unit_price', ''), ('discount', ''), ('tax_name', ''), ('tax_rate', ''), ('taxable_amount', ''),
        ('tax_amount', ''), ('line_total', ''), ('cost_at_sale', 'Your cost per unit when it was sold'),
    ], _sales_lines),
    ('sales_payments.csv', 'Money received against sales invoices (one row per payment).', [
        ('payment_id', ''), ('date', ''), ('invoice_id', 'Joins to sales.csv'), ('invoice_number', ''),
        ('method', 'Payment method name'), ('amount', ''),
        ('receipt_id', 'Filled when the payment came from a customer receipt (joins to customer_receipts.csv)'),
        ('recorded_by', 'Username'),
    ], _sales_payments),
    ('customer_receipts.csv', 'Lump-sum money customers paid you (spread over their invoices or kept as credit).', [
        ('receipt_id', ''), ('date', ''), ('customer_id', ''), ('customer_name', ''), ('method', ''),
        ('amount', ''), ('unallocated', 'The part that did not pay any invoice'),
        ('advance', 'The part kept as an advance'), ('note', ''), ('recorded_by', 'Username'),
    ], _customer_receipts),
    ('purchases.csv', 'Purchase invoices from your suppliers.', [
        ('purchase_id', 'Unique id (join key for purchase_lines)'), ('purchase_number', ''),
        ('supplier_reference', "The supplier's own invoice number"), ('date', ''),
        ('status', 'DRAFT / RECEIVED'), ('branch', ''), ('supplier_id', ''), ('supplier_name', ''),
        ('total_amount', ''), ('paid_amount', ''), ('notes', ''),
    ], _purchases),
    ('purchase_lines.csv', 'The items on each purchase invoice.', [
        ('line_id', ''), ('purchase_id', 'Joins to purchases.csv'), ('purchase_number', ''), ('sku', ''),
        ('product_name', ''), ('unit', 'The buying unit, empty = base unit'),
        ('unit_factor', 'How many base units one buying unit holds'), ('quantity', ''), ('unit_cost', ''),
        ('line_total', ''), ('expiry_date', ''), ('batch_number', ''),
    ], _purchase_lines),
    ('supplier_payments.csv', 'Money you paid your suppliers.', [
        ('payment_id', ''), ('date', ''), ('supplier_id', ''), ('supplier_name', ''),
        ('method', 'Payment method name (older payments may show CASH / BANK / CARD / OTHER)'),
        ('amount', ''), ('note', ''), ('recorded_by', 'Username'),
    ], _supplier_payments),
    ('refunds.csv', 'Returns / refunds (one row per refund).', [
        ('refund_id', 'Unique id (join key for refund_lines)'), ('refund_number', ''), ('date', ''),
        ('branch', ''), ('original_invoice_id', 'Joins to sales.csv'), ('original_invoice_number', ''),
        ('customer_id', ''), ('customer_name', ''), ('total_refunded', 'Value of the goods returned'),
        ('restocking_fee', ''), ('paid_back', 'Money handed back to the customer'),
        ('store_credit_back', 'Given back as store credit'), ('drawer_cash', 'Cash taken from the till'),
        ('refund_method', ''), ('reason', ''), ('recorded_by', 'Username'),
    ], _refunds),
    ('refund_lines.csv', 'The items on each refund.', [
        ('line_id', ''), ('refund_id', 'Joins to refunds.csv'), ('refund_number', ''), ('sku', ''),
        ('product_name', ''), ('unit_factor', ''), ('quantity', ''), ('refund_amount', ''),
        ('back_to_stock', 'true = the item went back into stock'),
    ], _refund_lines),
    ('expenses.csv', 'Shop expenses.', [
        ('expense_id', ''), ('date', ''), ('branch', ''), ('category', ''), ('amount', ''),
        ('description', ''),
    ], _expenses),
]

PRODUCTS_FILE = 'products.csv'


def _readme(store, tz, counts):
    out = [
        "VENDORYA - YOUR SHOP'S DATA",
        '=' * 30,
        f'Shop: {store.name}',
        f'Exported: {timezone.now().astimezone(tz).isoformat(timespec="seconds")}',
        '',
        'Every file is a CSV (comma separated), UTF-8 with a BOM so Excel shows Arabic correctly.',
        'Dates are ISO (2026-10-14T13:05:00+03:00) in the shop time zone. Money is a plain number (1234.50).',
        'Deleted records are NOT included. Ids let you join lines to their invoices.',
        '',
        f'{PRODUCTS_FILE}  ({counts.get(PRODUCTS_FILE, 0)} rows)',
        '  Your products, one row per variant - the same file as Inventory > Import / Export > Export,',
        '  so it can be edited and imported back. Columns: M_BRANCH, A_SUPP (supplier), P_NAME,',
        '  P_REF (groups variants of one product, only when needed), CAT_* (category levels),',
        '  <ATTRIBUTE>_DD/_FT/_NO (your attributes), Q_QTY (stock), W_PRICE (cost), R_PRICE (price), SKU, SKU2.',
        '',
    ]
    for name, desc, cols, _rows in TABLES:
        out.append(f'{name}  ({counts.get(name, 0)} rows)')
        out.append(f'  {desc}')
        for col, note in cols:
            out.append(f'  - {col}' + (f': {note}' if note else ''))
        out.append('')
    return '\r\n'.join(out)


def _write_csv(zf, name, header, rows, tz):
    """Stream one CSV into the zip, row by row. Returns the row count (header excluded)."""
    n = 0
    with io.TextIOWrapper(zf.open(name, 'w'), encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in rows:
            w.writerow([_cell(v, tz) for v in row])
            n += 1
    return n


def _store_tz(store):
    try:
        return ZoneInfo(store.timezone or 'UTC')
    except Exception:   # an unknown zone name must not break the export
        return ZoneInfo('UTC')


def build_shop_export(store):
    """Write the whole export zip into a temp file. Returns (file positioned at 0, {file name: row count})."""
    from inventory.import_export import export_catalog
    tz = _store_tz(store)
    counts = {}
    tmp = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    try:
        with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            catalog = export_catalog(store)
            zf.writestr(PRODUCTS_FILE, '﻿' + catalog)
            counts[PRODUCTS_FILE] = max(0, sum(1 for _ in csv.reader(io.StringIO(catalog))) - 1)
            del catalog
            for name, _desc, cols, rows in TABLES:
                counts[name] = _write_csv(zf, name, [c for c, _ in cols], rows(store), tz)
            zf.writestr('README.txt', _readme(store, tz, counts))
    except Exception:
        tmp.close()
        raise
    tmp.seek(0)
    return tmp, counts


def export_filename(store, today=None):
    code = re.sub(r'[^A-Za-z0-9_-]', '', str(store.store_code or '')) or 'shop'
    today = today or timezone.now().astimezone(_store_tz(store)).date()
    return f'vendorya-{code}-export-{today:%Y%m%d}.zip'


# ── the endpoint ─────────────────────────────────────────────────────────────

class ShopExportThrottle(SimpleRateThrottle):
    """One export per shop per minute — it reads every table, so a double-click must not run it twice."""
    scope = 'shop_export'
    rate = '1/min'

    def get_cache_key(self, request, view):
        store_id = getattr(getattr(request, 'user', None), 'store_id', None)
        if not store_id:
            return None
        return self.cache_format % {'scope': self.scope, 'ident': store_id}


class ShopExportViewSet(viewsets.ViewSet):
    """GET → the zip download. OWNER only; the platform admin (no shop) is refused."""
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {'list': 'OWNER'}
    throttle_classes = [ShopExportThrottle]

    def list(self, request):
        store = getattr(request.user, 'store', None)
        if store is None:
            raise PermissionDenied("This account has no shop to export.")
        fh, counts = build_shop_export(store)
        filename = export_filename(store)
        log_activity(request=request, op_type=ActivityLog.OperationType.OTHER,
                     action="Exported the shop's data (zip download)",
                     details={'file': filename, 'rows': counts})
        resp = FileResponse(fh, as_attachment=True, filename=filename, content_type='application/zip')
        resp['Cache-Control'] = 'no-store'
        return resp
