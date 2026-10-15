"""Return goods to a supplier (PROLINE-PARITY gap "purchase returns").

ONE door for both directions, each inside one transaction with the usual lock order (serials → stock rows):
- `create_return` posts at once: stock leaves the branch (never below zero — you can't send back what isn't here),
  expiry batches are drawn FEFO and remembered, serial units → SUPPLIER_RETURNED, the supplier's running account
  drops by the total (inventory.views.supplier_account_sources), and money handed back lands in a box (Treasury on).
- `void_return` undoes every one of those (same batches, same units, the box line reversed) and keeps the row as VOID.
A line linked to a purchase line takes that line's unit + cost and can't send back more than it brought in.
"""
import uuid
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from inventory.models import ProductVariant, ProductUnit, StockLevel, StockBatch, is_expiry_tracked, draw_from_batches
from inventory import serials as serial_engine
from treasury import ledger
from treasury.models import MoneyMove

from .models import (PaymentMethod, PurchaseInvoice, PurchaseItem, PurchaseReturn, PurchaseReturnItem,
                     PurchaseReturnSequence, weighted_avg_cost)
from .treasury_flows import _box_for, _live, _reverse_all, _shift_link, check_box, guarded, open_shift_of, refuse

ZERO = Decimal('0')
CENT = Decimal('0.01')


def _err(code, detail, **extra):
    return ValidationError({'code': code, 'detail': detail, **{k: str(v) for k, v in extra.items()}})


def _dec(value, field):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise _err('bad_number', f'{field} must be a number.', field=field)
    if not d.is_finite():
        raise _err('bad_number', f'{field} must be a number.', field=field)
    return d


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def returned_qty(purchase_item, exclude=None):
    """How much of a purchase line (in ITS unit) went back already, on returns not voided."""
    qs = PurchaseReturnItem.objects.filter(purchase_item=purchase_item,
                                           purchase_return__status=PurchaseReturn.Status.POSTED,
                                           purchase_return__is_deleted=False)
    if exclude is not None:
        qs = qs.exclude(purchase_return=exclude)
    return qs.aggregate(s=Sum('quantity'))['s'] or ZERO


def _next_number(store):
    seq, _ = PurchaseReturnSequence.objects.select_for_update().get_or_create(store=store)
    seq.last_number += 1
    seq.save(update_fields=['last_number'])
    return seq.last_number


def _line_spec(store, purchase, raw):
    """Resolve one input line → dict(variant, unit, factor, qty, unit_cost, purchase_item, serials)."""
    qty = _dec(raw.get('quantity'), 'quantity')
    if qty <= 0:
        raise _err('quantity_positive', 'Each line needs a quantity above zero.')
    serials = raw.get('serial_numbers') or []
    if not isinstance(serials, list):
        raise _err('bad_serials', 'serial_numbers must be a list.')
    pi_id = raw.get('purchase_item')
    if pi_id:
        if purchase is None:
            raise _err('purchase_required', 'A line from a purchase needs that purchase.')
        pi_id = _uuid(pi_id) if not isinstance(pi_id, int) and not str(pi_id).isdigit() else int(pi_id)
        pi = (PurchaseItem.objects.select_for_update(of=('self',)).select_related('variant__product', 'unit')
              .filter(pk=pi_id, invoice=purchase).first()) if pi_id else None
        if pi is None:
            raise _err('purchase_item_not_found', 'This line is not on that purchase.')
        left = Decimal(str(pi.quantity)) - returned_qty(pi)
        if qty > left:
            raise _err('more_than_bought', f'Only {left.normalize()} of {pi.variant.product.name} can still go back.',
                       product=pi.variant.product.name, left=left.normalize())
        return dict(variant=pi.variant, unit=pi.unit, factor=Decimal(str(pi.unit_factor or 1)), qty=qty,
                    unit_cost=Decimal(str(pi.unit_cost)), purchase_item=pi, serials=serials)
    vid = _uuid(raw.get('variant'))
    variant = ProductVariant.objects.select_related('product').filter(
        pk=vid, product__store=store).first() if vid else None
    if variant is None:
        raise _err('variant_not_found', 'Choose a product of this shop.')
    unit, factor = None, Decimal('1')
    if raw.get('unit'):
        unit = ProductUnit.objects.filter(pk=_uuid(raw['unit']), variant=variant).first() if _uuid(raw['unit']) else None
        if unit is None:
            raise _err('unit_not_found', 'That unit does not belong to this product.')
        factor = Decimal(str(unit.factor))
    if raw.get('unit_cost') in (None, ''):
        unit_cost = (weighted_avg_cost(variant, store) * factor).quantize(CENT)
    else:
        unit_cost = _dec(raw['unit_cost'], 'unit_cost')
        if unit_cost < 0:
            raise _err('cost_negative', 'A cost cannot be below zero.')
    return dict(variant=variant, unit=unit, factor=factor, qty=qty, unit_cost=unit_cost.quantize(CENT),
                purchase_item=None, serials=serials)


@transaction.atomic
def create_return(store, user, *, supplier, branch=None, purchase=None, lines=(), refund_amount=ZERO,
                  refund_method=None, refund_box=None, notes='', date=None):
    if supplier is None or supplier.store_id != store.id or supplier.is_deleted:
        raise _err('supplier_not_found', 'Choose a supplier of this shop.')
    if purchase is not None:
        if purchase.store_id != store.id or purchase.is_deleted:
            raise _err('purchase_not_found', 'Purchase not found.')
        if purchase.status != PurchaseInvoice.Status.RECEIVED:
            raise _err('purchase_not_received', 'Only a received purchase can be returned.')
        if purchase.supplier_id != supplier.id:
            raise _err('wrong_supplier', 'This purchase is from another supplier.')
        branch = branch or purchase.branch
    if branch is None or branch.store_id != store.id:
        raise _err('branch_not_found', 'Choose a branch of this shop.')
    if not lines:
        raise _err('no_lines', 'Add at least one product to send back.')

    refund_amount = _dec(refund_amount or 0, 'refund_amount').quantize(CENT)
    if refund_amount < 0:
        raise _err('refund_negative', 'Money back cannot be below zero.')

    # Locks first, same order as every other stock path: the shop's serial lock, then the stock rows.
    serial_engine.lock_shop_serials(store)
    specs = [_line_spec(store, purchase, raw) for raw in lines]

    ret = PurchaseReturn.all_objects.create(
        store=store, branch=branch, supplier=supplier, purchase=purchase, return_number=_next_number(store),
        notes=notes or '', created_by=user, **({'date': date} if date else {}))
    total = ZERO
    for sp in specs:
        item = PurchaseReturnItem.objects.create(
            purchase_return=ret, variant=sp['variant'], purchase_item=sp['purchase_item'], unit=sp['unit'],
            unit_factor=sp['factor'], quantity=sp['qty'], unit_cost=sp['unit_cost'], serial_numbers=sp['serials'])
        base = item.base_qty
        stock, _ = StockLevel.objects.select_for_update().get_or_create(variant=item.variant, branch=branch)
        have = Decimal(str(stock.quantity))
        if base > have:
            raise _err('not_enough_stock', f'Only {have.normalize()} of {item.variant.product.name} at {branch.name}.',
                       product=item.variant.product.name, available=have.normalize())
        stock.quantity = have - base
        stock.save()
        serial_engine.supplier_return(item, user=user)
        if is_expiry_tracked(item.variant):
            draws = draw_from_batches(item.variant, branch, base)
            PurchaseReturnItem.objects.filter(pk=item.pk).update(
                batch_draws=[{'batch': str(d['batch'].pk), 'qty': str(d['qty'])} for d in draws])
        total += item.total_cost
    ret.total_amount = total

    if refund_amount > total:
        raise _err('refund_above_total', 'Money back cannot be more than the return is worth.', total=total)
    if refund_amount > 0:
        if refund_method is None or refund_method.store_id != store.id or refund_method.is_deleted:
            raise _err('refund_method_required', 'Choose how the supplier paid the money back.')
        if refund_method.is_agel or refund_method.is_store_credit:
            raise _err('refund_method_not_money', 'Pick a real payment method for money handed back.')
        ret.refund_amount = refund_amount
        ret.refund_method = refund_method
        if ledger.is_on(store):
            with guarded():
                shift = open_shift_of(user, store)
                box = refund_box or _box_for(refund_method, store, shift=shift, branch=branch)
                check_box(box, store, 'refund_box')
                if box is None:
                    refuse('refund_box_required', 'Choose the money box the money went into.', field='refund_box')
                ret.refund_box = box
                ledger.post(box, refund_amount, MoneyMove.Kind.SUPPLIER_IN, user=user,
                            shift=_shift_link(box, shift), purchase_return=ret,
                            note=f'Return #{ret.return_number} — {supplier.name}')
    ret.save(update_fields=['total_amount', 'refund_amount', 'refund_method', 'refund_box', 'updated_at'])
    return ret


@transaction.atomic
def void_return(ret, user, reason):
    if not (reason or '').strip():
        raise _err('reason_required', 'Write why this return is voided.')
    serial_engine.lock_shop_serials(ret.store)
    ret = PurchaseReturn.all_objects.select_for_update().get(pk=ret.pk)
    if ret.status != PurchaseReturn.Status.POSTED:
        raise _err('already_void', 'This return is already voided.')
    for item in ret.items.select_related('variant__product'):
        stock, _ = StockLevel.objects.select_for_update().get_or_create(variant=item.variant, branch=ret.branch)
        stock.quantity = Decimal(str(stock.quantity)) + item.base_qty
        stock.save()
        serial_engine.void_supplier_return(item, user=user)
        for d in item.batch_draws or []:
            b = StockBatch.objects.select_for_update().filter(pk=d['batch']).first()
            if b is not None:
                b.quantity_remaining = Decimal(str(b.quantity_remaining)) + Decimal(d['qty'])
                b.save(update_fields=['quantity_remaining', 'updated_at'])
    with guarded():
        _reverse_all(_live(purchase_return=ret, kind=MoneyMove.Kind.SUPPLIER_IN), user,
                     f'Return #{ret.return_number} voided: {reason.strip()}', ret.store)
    ret.status = PurchaseReturn.Status.VOID
    ret.void_reason = reason.strip()
    ret.save(update_fields=['status', 'void_reason', 'updated_at'])
    return ret
