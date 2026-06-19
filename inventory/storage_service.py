"""Storage operations — the only sanctioned way stock moves to/from storage.

Storage is an *operational visibility filter*, never an accounting boundary:
moving to/from storage never touches P&L or cost basis. The only P&L event is a
write-off, which books a `StockAdjustment(DAMAGE)` (the accounting document).

Cost-layer model: `StorageStock` is one row per move-in event. Retrieval and
write-off consume layers FIFO (oldest `moved_in_at` first); an emptied layer is
soft-deleted. The AVCO cost snapshot is frozen at move-in (`variant.cost_price`).

All three operations are atomic + row-locked. They raise Django ``ValidationError``
on a business-rule breach; the calling view translates that to a clean HTTP 400.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    StockLevel, StockAdjustment, StorageStock, StorageMovement,
)


def _dq(value):
    """Coerce anything (float default, str, int) to a Decimal safely."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _money(value):
    return _dq(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _consume_layers_fifo(*, store, storage_location, variant, qty):
    """Lock the variant's layers at this location, validate enough is parked,
    then consume `qty` FIFO. Soft-deletes any layer that hits zero.

    Returns the weighted-average cost (Decimal) of the consumed quantity — the
    single `cost_at_move` figure recorded on the resulting movement.
    """
    qty = _dq(qty)
    if qty <= 0:
        raise ValidationError("Quantity must be greater than zero.")

    layers = list(
        StorageStock.objects
        .select_for_update()
        .filter(store=store, storage_location=storage_location, variant=variant)
        .order_by('moved_in_at')                       # FIFO
    )
    available = sum((_dq(l.quantity_remaining) for l in layers), Decimal('0'))
    if available < qty:
        raise ValidationError(
            f"Not enough in storage for {variant.sku}: "
            f"available {available}, requested {qty}."
        )

    remaining = qty
    cost_total = Decimal('0')
    for layer in layers:
        if remaining <= 0:
            break
        take = min(remaining, _dq(layer.quantity_remaining))
        cost_total += take * _dq(layer.cost_at_move)
        layer.quantity_remaining = _dq(layer.quantity_remaining) - take
        if layer.quantity_remaining <= 0:
            layer.is_deleted = True                     # emptied layer → soft-delete
        layer.save()
        remaining -= take

    weighted_cost = _money(cost_total / qty)
    return weighted_cost


@transaction.atomic
def move_to_storage(*, variant, qty, from_branch, storage_location, user,
                    reason=None, note=None):
    """Park `qty` of `variant` from `from_branch` into `storage_location`.

    Deducts active stock, opens a new cost layer (snapshotting `variant.cost_price`),
    and logs a TO_STORAGE movement. No P&L impact.
    """
    qty = _dq(qty)
    if qty <= 0:
        raise ValidationError("Quantity must be greater than zero.")

    # Storage is not yet batch/FEFO-aware: it tracks a variant + cost layer, not the
    # dated StockBatch sub-ledger. Parking an expiry-tracked variant here would
    # deduct StockLevel without drawing its batches, silently desyncing the cached
    # total from the batch sum. Block it cleanly until storage learns about batches.
    from .models import is_expiry_tracked
    if is_expiry_tracked(variant):
        raise ValidationError(
            f"{variant.sku} is expiry/batch-tracked and can't be moved to storage "
            f"yet — storage doesn't track batch expiry. Sell or adjust it from its "
            f"branch instead."
        )

    store = storage_location.store
    stock = (StockLevel.objects.select_for_update()
             .filter(variant=variant, branch=from_branch).first())
    available = _dq(stock.quantity) if stock else Decimal('0')
    if available < qty:
        # Moving out of active never exceeds what's on hand, regardless of the
        # store's negative-stock policy — you can't park what isn't there.
        raise ValidationError(
            f"Not enough active stock for {variant.sku} at {from_branch.name}: "
            f"available {available}, requested {qty}."
        )

    cost = _money(variant.cost_price)

    stock.quantity = available - qty
    stock.save()

    StorageStock.objects.create(
        store=store,
        storage_location=storage_location,
        variant=variant,
        quantity_remaining=qty,
        cost_at_move=cost,
        moved_in_at=timezone.now(),
    )

    return StorageMovement.objects.create(
        store=store,
        storage_location=storage_location,
        variant=variant,
        direction=StorageMovement.Direction.TO_STORAGE,
        quantity=qty,
        cost_at_move=cost,
        from_branch=from_branch,
        created_by=user,
        reason=reason or '',
        note=note or '',
    )


@transaction.atomic
def retrieve_from_storage(*, variant, qty, storage_location, to_branch, user,
                          reason=None, note=None):
    """Bring `qty` of `variant` back from `storage_location` into `to_branch`.

    Consumes layers FIFO and credits active stock. No P&L impact.
    """
    store = storage_location.store
    weighted_cost = _consume_layers_fifo(
        store=store, storage_location=storage_location, variant=variant, qty=qty,
    )
    qty = _dq(qty)

    stock, _ = (StockLevel.objects.select_for_update()
                .get_or_create(variant=variant, branch=to_branch))
    stock.quantity = _dq(stock.quantity) + qty
    stock.save()

    return StorageMovement.objects.create(
        store=store,
        storage_location=storage_location,
        variant=variant,
        direction=StorageMovement.Direction.FROM_STORAGE,
        quantity=qty,
        cost_at_move=weighted_cost,
        to_branch=to_branch,
        created_by=user,
        reason=reason or '',
        note=note or '',
    )


@transaction.atomic
def write_off_from_storage(*, variant, qty, storage_location, branch, reason,
                           user, note=None):
    """Write off `qty` of `variant` from `storage_location` as damage/disposal.

    This is the only storage op that touches P&L. Modeled as *retrieve-then-
    damage*: the storage layer is consumed, the qty is credited to `branch`'s
    active stock, then a `StockAdjustment(DAMAGE, -qty)` removes it — so active
    stock nets to zero while a proper P&L document (the adjustment) is booked.
    The WRITE_OFF movement links back to that adjustment.
    """
    store = storage_location.store
    weighted_cost = _consume_layers_fifo(
        store=store, storage_location=storage_location, variant=variant, qty=qty,
    )
    qty = _dq(qty)

    # Retrieve leg: bring it back to active so the DAMAGE adjustment has stock to
    # remove (and obeys the same row-locked, negative-stock-safe path as the POS).
    stock, _ = (StockLevel.objects.select_for_update()
                .get_or_create(variant=variant, branch=branch))
    stock.quantity = _dq(stock.quantity) + qty
    stock.save()

    adjustment = StockAdjustment(
        store=store,
        branch=branch,
        variant=variant,
        quantity_change=-qty,
        reason=StockAdjustment.Reason.DAMAGE,
        notes=(reason or 'Storage write-off'),
        adjusted_by=user,
    )
    adjustment.save()       # deducts the qty back out of active stock (net zero)

    return StorageMovement.objects.create(
        store=store,
        storage_location=storage_location,
        variant=variant,
        direction=StorageMovement.Direction.WRITE_OFF,
        quantity=qty,
        cost_at_move=weighted_cost,
        from_branch=branch,
        created_by=user,
        reason=reason or '',
        note=note or '',
        related_adjustment=adjustment,
    )
