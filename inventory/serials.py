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

def untracked_sold(store, variant, original=None, exclude_refund_item=None):
    """Units of `variant` sold WITHOUT a serial (before tracking was switched on) that have not come back yet —
    on `original` when given, else across the shop. = base units sold on POSTED sales − units sold by serial
    there − units already returned without one of those serials. Those units may come back with no serial."""
    from django.db.models import F, Sum
    from finance.models import SalesInvoiceItem, RefundItem
    lines = SalesInvoiceItem.objects.filter(variant=variant, invoice__store=store, invoice__status='POSTED',
                                            invoice__is_deleted=False)
    refunds = RefundItem.objects.filter(variant=variant, refund__store=store, refund__is_deleted=False)
    moves = SerialMove.all_objects.filter(store=store, unit__variant=variant)
    if original is not None:
        lines = lines.filter(invoice=original)
        refunds = refunds.filter(refund__original_invoice=original)
        moves = moves.filter(sale_item__invoice=original)
    else:
        moves = moves.filter(sale_item__invoice__status='POSTED', sale_item__invoice__is_deleted=False)
    if exclude_refund_item is not None:
        refunds = refunds.exclude(pk=exclude_refund_item.pk)
    sold = lines.aggregate(q=Sum(F('quantity') * F('unit_factor')))['q'] or Decimal('0')
    returned = refunds.aggregate(q=Sum(F('quantity') * F('unit_factor')))['q'] or Decimal('0')
    if exclude_refund_item is not None:
        moves_returned = moves.filter(kind=SerialMove.Kind.RETURNED).exclude(refund_item=exclude_refund_item)
    else:
        moves_returned = moves.filter(kind=SerialMove.Kind.RETURNED)
    # s168 review: only serial returns that belong to the refunds counted above — a no-receipt return of a unit
    # sold on `original` still carries its sale line, and counting it here would free a phantom serial-less unit.
    moves_returned = moves_returned.filter(refund_item__in=refunds)
    sold_by_serial = moves.filter(kind=SerialMove.Kind.SOLD).count()
    returned_by_serial = moves_returned.count()   # returns of a unit SOLD by serial (they carry its sale line)
    pre_sold = Decimal(str(sold)) - sold_by_serial
    pre_returned = max(Decimal(str(returned)) - returned_by_serial, Decimal('0'))
    return max(int((pre_sold - pre_returned).to_integral_value(rounding='ROUND_FLOOR')), 0)


def return_units(refund_item, original=None, user=None):
    """A refund line of a tracked product: the units coming back, each SOLD by this shop for this product — on
    `original` when the return names its sale. Restocked → IN_STOCK at the refund's branch (sellable again;
    StockLevel went up in RefundItem.save). Not restocked → RETURNED (kept apart, not sellable). Either way
    the warranty of that sale ends here.

    A unit sold BEFORE tracking was switched on has no serial row: up to `untracked_sold` such units may come
    back with no serial (stock goes up without one — "serials ≤ stock" still holds), or with a serial typed
    now, which becomes a new unit row (the shop can sell it by serial from then on)."""
    from finance.models import RefundItem
    variant = refund_item.variant
    if not is_tracked(variant):
        return
    refund = refund_item.refund
    name = variant.product.name
    need = whole_units(name, refund_item.quantity, refund_item.unit_factor)
    serials = clean_list(refund_item.serial_numbers, name)
    if len(serials) > need:
        _check_count(name, serials, need)
    restock = refund_item.restock_inventory
    if restock:
        lock_shop_serials(refund.store)   # normally already held (RefundInvoiceSerializer.create takes it first)
    qs = SerialUnit.all_objects.select_for_update(of=('self',)).filter(
        store=refund.store, variant=variant, serial__in=serials,
        status=SerialUnit.Status.SOLD, is_deleted=False)
    if original is not None:
        qs = qs.filter(sale_item__invoice=original)
    units = {u.serial: u for u in qs}
    typed_new = [s for s in serials if s not in units]           # serials for units sold before tracking
    no_row_slots = need - (len(serials) - len(typed_new))        # units coming back that have no SOLD row
    if no_row_slots > 0:
        room = untracked_sold(refund.store, variant, original, exclude_refund_item=refund_item)
        if no_row_slots > room:
            if typed_new:
                where = f'on invoice #{original.invoice_number}' if original is not None else 'by this shop'
                raise _err('serial_not_sold', f'Not sold {where}: {", ".join(sorted(typed_new))}.',
                           serials=sorted(typed_new), product=name)
            raise _err('serial_count', f'{name} needs exactly {need} serial number(s), one per unit — '
                                       f'got {len(serials)}.', product=name, need=need, got=len(serials))
    if typed_new:
        # A serial typed for a pre-tracking unit must not be a unit we sold BY SERIAL (another sale / mistyped).
        sold_elsewhere = sorted(SerialUnit.all_objects.filter(
            store=refund.store, serial__in=typed_new, status=SerialUnit.Status.SOLD, is_deleted=False,
        ).values_list('serial', flat=True))
        if sold_elsewhere:
            raise _err('serial_not_sold', f'Not sold on this invoice: {", ".join(sold_elsewhere)}.',
                       serials=sold_elsewhere, product=name)
    if restock:
        _refuse_if_on_shelf(refund.store, serials, name)
    status = SerialUnit.Status.IN_STOCK if restock else SerialUnit.Status.RETURNED
    for s in serials:
        if s in units:
            u = units[s]
            sold_on = u.sale_item
            u.status = status
            u.branch = refund.branch
            _clear_sale(u)
            u.save(update_fields=_SALE_FIELDS)
        else:
            u, sold_on = SerialUnit.all_objects.create(store=refund.store, variant=variant, branch=refund.branch,
                                                       serial=s, status=status), None
        _move(u, SerialMove.Kind.RETURNED, branch=refund.branch, sale_item=sold_on,
              refund_item=refund_item, created_by=user,
              note='' if sold_on is not None else 'sold before serial tracking')
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


# ── return to supplier (finance/purchase_returns.py) ───────────────────────

def supplier_return(item, user=None):
    """A return-to-supplier line of a tracked product: one serial per unit; each listed unit must be IN_STOCK at the
    return's branch → SUPPLIER_RETURNED. On a return linked to a purchase line, every unit must be one that line
    received (you send back what you bought from THEM)."""
    ret = item.purchase_return
    variant = item.variant
    if not is_tracked(variant):
        return
    name = variant.product.name
    need = whole_units(name, item.base_qty)
    serials = clean_list(item.serial_numbers, name)
    _check_count(name, serials, need)
    lock_shop_serials(ret.store)
    units = _in_stock_units(ret.store, variant, ret.branch, serials, name)
    if item.purchase_item_id:
        # A unit registered later on stock that was here before tracking ("Register serials") has no purchase
        # line — it may go back on any purchase of this product; a unit another purchase brought in may not.
        foreign = [u.serial for u in units if u.purchase_item_id not in (None, item.purchase_item_id)]
        if foreign:
            raise _err('serial_not_from_purchase', f'Not received on this purchase: {", ".join(foreign)}.',
                       serials=foreign, product=name)
    for u in units:
        u.status = SerialUnit.Status.SUPPLIER_RETURNED
        u.save(update_fields=['status', 'updated_at'])
        _move(u, SerialMove.Kind.TO_SUPPLIER, branch=ret.branch, purchase_return_item=item, created_by=user,
              note=f'Return #{ret.return_number}')
    type(item).objects.filter(pk=item.pk).update(serial_numbers=serials)


def void_supplier_return(item, user=None):
    """Undo supplier_return: the units it sent back come back IN_STOCK at the return's branch."""
    ret = item.purchase_return
    if not is_tracked(item.variant) or not item.serial_numbers:
        return
    lock_shop_serials(ret.store)
    units = SerialUnit.all_objects.select_for_update().filter(
        store=ret.store, variant=item.variant, serial__in=item.serial_numbers,
        status=SerialUnit.Status.SUPPLIER_RETURNED, is_deleted=False)
    if units.count() != len(item.serial_numbers):
        raise _err('serial_not_available', 'Some units of this return are no longer marked as returned.',
                   product=item.variant.product.name)
    for u in units:
        u.status = SerialUnit.Status.IN_STOCK
        u.branch = ret.branch
        u.save(update_fields=['status', 'branch', 'updated_at'])
        _move(u, SerialMove.Kind.RETURN_VOIDED, branch=ret.branch, purchase_return_item=item, created_by=user,
              note=f'Return #{ret.return_number} voided')


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
    returned / written off). None when the shop never had it. Soft-deleted (removed) rows are ignored.
    ONE pick rule for one serial and for a list screen: this goes through find_serials."""
    s = normalize(serial)
    if not s or store is None:
        return None
    return find_serials(store, [s]).get(s)


def warranty_active(unit):
    """THE rule for "this unit is still under warranty" (Serial Lookup page + service desk pre-tick): a SOLD
    unit whose warranty ends today or later (shop's local date)."""
    return bool(unit is not None and unit.status == SerialUnit.Status.SOLD and unit.warranty_ends_on
                and unit.warranty_ends_on >= timezone.localdate())


def find_serials(store, serials):
    """find_serial for many serials in ONE query (a list screen): {normalized serial: unit}. Same pick — the
    unit on the shelf, else the most recent row. Serials the shop never had are simply absent."""
    wanted = {normalize(s) for s in serials} - {''}
    if not wanted or store is None:
        return {}
    out = {}
    for u in (SerialUnit.all_objects.filter(store=store, serial__in=wanted, is_deleted=False)
              .select_related('variant__product', 'branch', 'sale_item__invoice').order_by('-created_at')):
        have = out.get(u.serial)
        if have is None or (u.status == SerialUnit.Status.IN_STOCK and have.status != SerialUnit.Status.IN_STOCK):
            out[u.serial] = u
    return out


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
