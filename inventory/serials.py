"""Serial / IMEI / service tag per unit (PROLINE-PARITY New 7) + warranty per sold unit (New 8).

Serials are NOT a stock path. StockLevel is still the on-hand number and still moves only through
purchase receive, sales checkout, refund, stock adjustment and stock transfer. Each of those calls in
here, INSIDE its own locked transaction, so the units ride along with the quantity:

    receive_purchase(invoice)        purchase DRAFT → RECEIVED      units created IN_STOCK at the branch
    post_sale(invoice)               sale DRAFT → POSTED            picked units → SOLD (+ warranty)
    void_sale(invoice)               sale POSTED → VOID             its SOLD units → back IN_STOCK
    return_units(refund_item, ...)   refund line saved              sold units → IN_STOCK (or RETURNED)
    apply_adjustment(adjustment)     stock adjustment saved         gain → new units, loss → WRITTEN_OFF
    transfer_item(item, transfer)    transfer line saved            units change branch

and two manager tools that never touch the quantity:

    register(...)   give serials to units already on hand (stock from before tracking was switched on,
                    or an import) — capped so in-stock serials never exceed StockLevel at that branch
    remove(...)     take back a wrongly registered serial (soft delete, IN_STOCK only)

Rules: base unit only (a pack of laptops makes no sense), whole quantities, one serial per unit, a serial
on the shelf only once per shop (DB constraint). Rows are locked with select_for_update so two tills
can't sell the same unit; every serial CREATION also takes a per-shop advisory lock (lock_shop_serials)
so two receives can't both pass the "already in stock?" check.

Every error is a DRF ValidationError carrying `serial_code` (+ `serials`, `product`, numbers) so the
screen can word it in the shop's language; `detail` is the English fallback.

`find_serial(store, serial)` is the public lookup other apps (the service desk) should use.
"""
import calendar
from decimal import Decimal

from django.db import connection
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventory.models import SerialUnit, SerialMove, StockLevel

MAX_SERIAL_LEN = 100
_LOCK_NS = 7312   # advisory-lock namespace for "serial creations of one shop" (7311 = barcodes)


# ── small helpers ───────────────────────────────────────────────────────────

def normalize(value):
    """A serial as stored and compared: no spaces anywhere (IMEIs are often printed in groups), UPPER-CASE
    (service tags are case-free; a scanner and a typist must land on the same unit)."""
    return ''.join(str(value or '').split()).upper()


def _err(code, detail, **extra):
    return ValidationError({'detail': detail, 'serial_code': code, **extra})


def clean_list(values, product_name=''):
    """Normalise a list of serials (or one text blob: newline / comma / semicolon separated — a pasted
    column). Drops blanks; refuses a serial typed twice or one longer than the field."""
    if values is None:
        return []
    if isinstance(values, str):
        for sep in (',', ';', '\r'):
            values = values.replace(sep, '\n')
        values = values.split('\n')
    out, seen = [], set()
    for raw in values:
        s = normalize(raw)
        if not s:
            continue
        if len(s) > MAX_SERIAL_LEN:
            raise _err('serial_too_long', f'Serial "{s[:20]}…" is longer than {MAX_SERIAL_LEN} characters.',
                       serials=[s[:40]], product=product_name)
        if s in seen:
            raise _err('serial_duplicate', f'Serial {s} is entered twice.', serials=[s], product=product_name)
        seen.add(s)
        out.append(s)
    return out


def is_tracked(variant):
    """True when this variant's product sells one serial-numbered unit at a time."""
    return bool(getattr(getattr(variant, 'product', None), 'track_serials', False))


def whole_units(product_name, quantity, unit_factor=1):
    """The number of serials a line needs. Refuses packs (factor ≠ 1) and fractions."""
    factor = Decimal(str(unit_factor or 1))
    if factor != 1:
        raise _err('serial_unit', f'{product_name} has serial numbers — it is bought and sold one unit at a time, '
                                  f'not in packs.', product=product_name)
    qty = Decimal(str(quantity or 0))
    if qty != qty.to_integral_value() or qty < 0:
        raise _err('serial_whole', f'{product_name} has serial numbers — use whole units (got {qty}).',
                   product=product_name)
    return int(qty)


def _check_count(product_name, serials, need):
    if len(serials) != need:
        raise _err('serial_count', f'{product_name} needs exactly {need} serial number(s), one per unit — '
                                   f'got {len(serials)}.', product=product_name, need=need, got=len(serials))


def lock_shop_serials(store):
    """Serialise serial CREATIONS of one shop until the transaction ends — a read-then-insert check
    ("is it already on the shelf?") would otherwise let two receives at the same instant both pass and
    the second hit the unique constraint as a 500. No-op outside Postgres / outside a transaction."""
    if store is None or connection.vendor != 'postgresql' or not connection.in_atomic_block:
        return
    with connection.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(%s, hashtext(%s))', [_LOCK_NS, str(store.pk)])


def _refuse_if_on_shelf(store, serials, product_name=''):
    taken = sorted(SerialUnit.all_objects.filter(
        store=store, serial__in=list(serials), status=SerialUnit.Status.IN_STOCK, is_deleted=False,
    ).values_list('serial', flat=True))
    if taken:
        raise _err('serial_in_stock', f'Already in stock in this shop: {", ".join(taken)}.',
                   serials=taken, product=product_name)


def add_months(day, months):
    """`day` + whole months, clamped to the month's last day (Jan 31 + 1 → Feb 28/29)."""
    m = day.month - 1 + int(months)
    year, month = day.year + m // 12, m % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def _local_day(dt):
    if dt is None:
        return timezone.localdate()
    if hasattr(dt, 'tzinfo'):
        return timezone.localdate(dt) if timezone.is_aware(dt) else dt.date()
    return dt


def _move(unit, kind, **kw):
    return SerialMove.all_objects.create(store_id=unit.store_id, unit=unit, kind=kind, **kw)


def _clear_sale(unit):
    unit.sale_item = None
    unit.sold_at = None
    unit.warranty_months = 0
    unit.warranty_ends_on = None


_SALE_FIELDS = ['status', 'branch', 'sale_item', 'sold_at', 'warranty_months', 'warranty_ends_on', 'updated_at']


# ── purchase receive ────────────────────────────────────────────────────────

def receive_purchase(invoice, user=None):
    """Purchase DRAFT → RECEIVED: every tracked line must carry exactly one serial per unit; each becomes an
    IN_STOCK unit at the purchase's branch. Called from handle_purchase_stock inside its transaction."""
    from finance.models import PurchaseItem
    items = [it for it in invoice.items.select_related('variant__product') if is_tracked(it.variant)]
    if not items:
        return
    lock_shop_serials(invoice.store)
    plan, seen = [], set()
    for it in items:
        name = it.variant.product.name
        need = whole_units(name, it.quantity, it.unit_factor)
        serials = clean_list(it.serial_numbers, name)
        _check_count(name, serials, need)
        twice = sorted(seen & set(serials))
        if twice:
            raise _err('serial_duplicate', f'Serial {", ".join(twice)} is on two lines.', serials=twice, product=name)
        seen |= set(serials)
        plan.append((it, serials))
    _refuse_if_on_shelf(invoice.store, seen)
    for it, serials in plan:
        for s in serials:
            unit = SerialUnit.all_objects.create(
                store=invoice.store, variant=it.variant, branch=invoice.branch, serial=s,
                status=SerialUnit.Status.IN_STOCK, purchase_item=it)
            _move(unit, SerialMove.Kind.RECEIVED, branch=invoice.branch, purchase_item=it, created_by=user)
        PurchaseItem.objects.filter(pk=it.pk).update(serial_numbers=serials)   # .update(): no total re-save


# ── sale ────────────────────────────────────────────────────────────────────

def post_sale(invoice, user=None):
    """Sale DRAFT → POSTED. For EVERY line: freeze the product's condition + warranty (New 8). For a tracked
    line: the picked serials must be exactly one per unit, IN_STOCK, of this product, at this sale's branch
    — rows locked, so a second till selling the same unit waits, then finds it SOLD and is refused.
    Called from handle_sale_stock inside its transaction (every posting path goes through it)."""
    from finance.models import SalesInvoiceItem
    items = list(invoice.items.select_related('variant__product'))
    sale_day = _local_day(invoice.date)
    plan, seen = [], set()
    for it in items:
        product = it.variant.product
        months = int(product.warranty_months or 0)
        snap = {'condition': product.condition or '', 'warranty_months': months,
                'warranty_ends_on': add_months(sale_day, months) if months else None}
        serials = []
        if is_tracked(it.variant):
            need = whole_units(product.name, it.quantity, it.unit_factor)
            serials = clean_list(it.serial_numbers, product.name)
            _check_count(product.name, serials, need)
            twice = sorted(seen & set(serials))
            if twice:
                raise _err('serial_duplicate', f'Serial {", ".join(twice)} is on two lines.',
                           serials=twice, product=product.name)
            seen |= set(serials)
        plan.append((it, serials, snap))

    units = {}
    if seen:
        units = {u.serial: u for u in SerialUnit.all_objects.select_for_update().filter(
            store=invoice.store, serial__in=list(seen), status=SerialUnit.Status.IN_STOCK, is_deleted=False)}
        missing = sorted(s for it, serials, _ in plan for s in serials
                         if s not in units or units[s].variant_id != it.variant_id
                         or units[s].branch_id != invoice.branch_id)
        if missing:
            branch = getattr(invoice.branch, 'name', '')
            raise _err('serial_not_available', f'Not in stock at {branch}: {", ".join(missing)}.',
                       serials=missing, branch=branch)

    for it, serials, snap in plan:
        for s in serials:
            u = units[s]
            u.status = SerialUnit.Status.SOLD
            u.sale_item = it
            u.sold_at = invoice.date
            u.warranty_months = snap['warranty_months']
            u.warranty_ends_on = snap['warranty_ends_on']
            u.save(update_fields=_SALE_FIELDS)
            _move(u, SerialMove.Kind.SOLD, branch=invoice.branch, sale_item=it, created_by=user)
        SalesInvoiceItem.objects.filter(pk=it.pk).update(serial_numbers=serials, **snap)


def void_sale(invoice, user=None):
    """Sale POSTED → VOID: the units it sold (and that were not returned since) go back on the shelf."""
    units = list(SerialUnit.all_objects.select_for_update(of=('self',)).filter(
        sale_item__invoice=invoice, status=SerialUnit.Status.SOLD, is_deleted=False))
    if not units:
        return
    lock_shop_serials(invoice.store)
    _refuse_if_on_shelf(invoice.store, {u.serial for u in units})
    for u in units:
        item = u.sale_item
        u.status = SerialUnit.Status.IN_STOCK
        _clear_sale(u)
        u.save(update_fields=_SALE_FIELDS)
        _move(u, SerialMove.Kind.VOIDED, branch=u.branch, sale_item=item, created_by=user)


# ── refund ──────────────────────────────────────────────────────────────────

def return_units(refund_item, original=None, user=None):
    """A refund line of a tracked product: the serials coming back, one per unit, each SOLD by this shop for
    this product — on `original` when the return names its sale. Restocked → IN_STOCK at the refund's
    branch (sellable again; StockLevel went up in RefundItem.save). Not restocked → RETURNED (kept apart,
    not sellable). Either way the warranty of that sale ends here."""
    from finance.models import RefundItem
    variant = refund_item.variant
    if not is_tracked(variant):
        return
    refund = refund_item.refund
    name = variant.product.name
    need = whole_units(name, refund_item.quantity, refund_item.unit_factor)
    serials = clean_list(refund_item.serial_numbers, name)
    _check_count(name, serials, need)
    qs = SerialUnit.all_objects.select_for_update(of=('self',)).filter(
        store=refund.store, variant=variant, serial__in=serials,
        status=SerialUnit.Status.SOLD, is_deleted=False)
    if original is not None:
        qs = qs.filter(sale_item__invoice=original)
    units = {u.serial: u for u in qs}
    missing = sorted(set(serials) - set(units))
    if missing:
        where = f'on invoice #{original.invoice_number}' if original is not None else 'by this shop'
        raise _err('serial_not_sold', f'Not sold {where}: {", ".join(missing)}.', serials=missing, product=name)
    if refund_item.restock_inventory:
        lock_shop_serials(refund.store)
        _refuse_if_on_shelf(refund.store, serials, name)
    status = SerialUnit.Status.IN_STOCK if refund_item.restock_inventory else SerialUnit.Status.RETURNED
    for s in serials:
        u = units[s]
        sold_on = u.sale_item
        u.status = status
        u.branch = refund.branch
        _clear_sale(u)
        u.save(update_fields=_SALE_FIELDS)
        _move(u, SerialMove.Kind.RETURNED, branch=refund.branch, sale_item=sold_on,
              refund_item=refund_item, created_by=user)
    RefundItem.objects.filter(pk=refund_item.pk).update(serial_numbers=serials)


# ── stock adjustment + transfer ────────────────────────────────────────────

def apply_adjustment(adjustment):
    """A stock adjustment of a tracked product (StockAdjustment.save, after StockLevel moved): a gain adds
    the listed units IN_STOCK at the branch, a loss writes the listed IN_STOCK units off."""
    from inventory.models import StockAdjustment
    variant = adjustment.variant
    if not is_tracked(variant):
        return
    name = variant.product.name
    change = Decimal(str(adjustment.quantity_change))
    need = whole_units(name, abs(change))
    serials = clean_list(adjustment.serial_numbers, name)
    _check_count(name, serials, need)
    user = adjustment.adjusted_by
    if change > 0:
        lock_shop_serials(adjustment.store)
        _refuse_if_on_shelf(adjustment.store, serials, name)
        for s in serials:
            u = SerialUnit.all_objects.create(store=adjustment.store, variant=variant, branch=adjustment.branch,
                                              serial=s, status=SerialUnit.Status.IN_STOCK)
            _move(u, SerialMove.Kind.ADJUSTED_IN, branch=adjustment.branch, adjustment=adjustment,
                  created_by=user, note=adjustment.reason)
    elif change < 0:
        units = _in_stock_units(adjustment.store, variant, adjustment.branch, serials, name)
        for u in units:
            u.status = SerialUnit.Status.WRITTEN_OFF
            u.save(update_fields=['status', 'updated_at'])
            _move(u, SerialMove.Kind.ADJUSTED_OUT, branch=adjustment.branch, adjustment=adjustment,
                  created_by=user, note=adjustment.reason)
    StockAdjustment.all_objects.filter(pk=adjustment.pk).update(serial_numbers=serials)


def transfer_item(item, transfer):
    """A transfer line of a tracked product: the listed units leave the source branch for the destination."""
    from inventory.models import StockTransferItem
    variant = item.variant
    if not is_tracked(variant):
        return
    name = variant.product.name
    need = whole_units(name, item.quantity)
    serials = clean_list(item.serial_numbers, name)
    _check_count(name, serials, need)
    for u in _in_stock_units(transfer.store, variant, transfer.from_branch, serials, name):
        u.branch = transfer.to_branch
        u.save(update_fields=['branch', 'updated_at'])
        _move(u, SerialMove.Kind.TRANSFERRED, branch=transfer.from_branch, to_branch=transfer.to_branch,
              transfer=transfer, created_by=transfer.transferred_by)
    StockTransferItem.objects.filter(pk=item.pk).update(serial_numbers=serials)


def _in_stock_units(store, variant, branch, serials, name):
    """Lock + return the listed units, refusing any that is not IN_STOCK, of `variant`, at `branch`."""
    units = {u.serial: u for u in SerialUnit.all_objects.select_for_update().filter(
        store=store, variant=variant, branch=branch, serial__in=serials,
        status=SerialUnit.Status.IN_STOCK, is_deleted=False)}
    missing = sorted(set(serials) - set(units))
    if missing:
        bname = getattr(branch, 'name', '')
        raise _err('serial_not_available', f'Not in stock at {bname}: {", ".join(missing)}.',
                   serials=missing, branch=bname, product=name)
    return [units[s] for s in serials]


# ── manager tools (no quantity change) ──────────────────────────────────────

def untracked_on_hand(variant, branch, lock=False):
    """Units on hand at `branch` that have no serial yet = StockLevel − units IN_STOCK there (≥ 0)."""
    qs = StockLevel.objects.filter(variant=variant, branch=branch)
    if lock:
        qs = qs.select_for_update()
    stock = qs.first()
    on_hand = Decimal(str(stock.quantity)) if stock else Decimal('0')
    with_serial = SerialUnit.all_objects.filter(variant=variant, branch=branch, is_deleted=False,
                                                status=SerialUnit.Status.IN_STOCK).count()
    return max(int(on_hand.to_integral_value(rounding='ROUND_FLOOR')) - with_serial, 0)


def register(store, variant, branch, serials, user=None, note=''):
    """Give serials to units ALREADY on hand (stock from before tracking was switched on / an import).
    Never moves stock: refused beyond the units on hand that have no serial yet. Run inside a transaction."""
    name = variant.product.name
    if not is_tracked(variant):
        raise _err('serial_off', f'{name} does not track serial numbers.', product=name)
    serials = clean_list(serials, name)
    if not serials:
        raise _err('serial_count', 'Enter at least one serial number.', product=name, need=1, got=0)
    lock_shop_serials(store)
    room = untracked_on_hand(variant, branch, lock=True)
    if len(serials) > room:
        raise _err('serial_register_cap',
                   f'{branch.name} has only {room} unit(s) of {name} without a serial — receive a purchase or '
                   f'make a stock adjustment to add more.', product=name, room=room, got=len(serials))
    _refuse_if_on_shelf(store, serials, name)
    created = []
    for s in serials:
        u = SerialUnit.all_objects.create(store=store, variant=variant, branch=branch, serial=s,
                                          status=SerialUnit.Status.IN_STOCK)
        _move(u, SerialMove.Kind.REGISTERED, branch=branch, created_by=user, note=(note or '')[:255])
        created.append(u)
    return created


def remove(unit, user=None, note=''):
    """Take back a wrongly registered / typed serial — only while it is IN_STOCK; the unit stays on hand
    (StockLevel untouched) and can be registered again with the right serial. Soft delete."""
    locked = SerialUnit.all_objects.select_for_update().get(pk=unit.pk)
    if locked.status != SerialUnit.Status.IN_STOCK or locked.is_deleted:
        raise _err('serial_not_available', f'{locked.serial} is not in stock — it can no longer be removed.',
                   serials=[locked.serial])
    locked.is_deleted = True
    locked.deleted_at = timezone.now()
    locked.save(update_fields=['is_deleted', 'deleted_at', 'updated_at'])
    _move(locked, SerialMove.Kind.REMOVED, branch=locked.branch, created_by=user, note=(note or '')[:255])
    return locked


# ── lookups (public — the service desk links here) ─────────────────────────

def find_serial(store, serial):
    """The unit a serial means in `store`: the one on the shelf if any, else the most recent row (sold /
    returned / written off). None when the shop never had it. Soft-deleted (removed) rows are ignored."""
    s = normalize(serial)
    if not s or store is None:
        return None
    qs = SerialUnit.all_objects.filter(store=store, serial=s, is_deleted=False).select_related(
        'variant__product', 'branch')
    return (qs.filter(status=SerialUnit.Status.IN_STOCK).first()
            or qs.order_by('-created_at').first())


def in_stock_unit(store, serial):
    """The unit on the shelf with this exact serial, or None (the POS scan)."""
    s = normalize(serial)
    if not s or store is None:
        return None
    return (SerialUnit.all_objects.filter(store=store, serial=s, status=SerialUnit.Status.IN_STOCK,
                                          is_deleted=False)
            .select_related('variant__product').first())


def has_units_on_shelf(product):
    return SerialUnit.all_objects.filter(variant__product=product, status=SerialUnit.Status.IN_STOCK,
                                         is_deleted=False).exists()
