"""Completing a sale — the ONE code path behind the till's checkout and a reservation becoming a sale (s172).

`post_sale(invoice, user, data, idem_key)` judges a DRAFT sale by every shop policy (open shift, split payment, Ajel +
credit limit, plan quota, oversell incl. units held by reservations, expired stock, serials), then flips it to POSTED
(→ the stock-out + numbering signals) and records the payments, all in one transaction. Moved verbatim from
SalesInvoiceViewSet.checkout; its early checks (posted / void / empty) stay in the caller."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import APIException, ValidationError

from inventory.models import StockLevel
from .models import Payment, Reservation, ReservationItem, SalesInvoice


class CheckoutRefused(APIException):
    """A 400 with a FLAT body `{"detail": "..."}` — the shape the till already reads (a ValidationError would wrap
    every value in a list)."""
    status_code = 400
    default_code = 'checkout'


def held_qty(variant, branch, exclude=None):
    """Base units of `variant` held at `branch` by OPEN reservations (minus `exclude`, a reservation being sold)."""
    qs = ReservationItem.objects.filter(variant=variant, reservation__branch=branch,
                                        reservation__status=Reservation.Status.OPEN,
                                        reservation__is_deleted=False)
    if exclude is not None:
        qs = qs.exclude(reservation=exclude)
    return qs.aggregate(s=Sum('quantity'))['s'] or Decimal('0')


def held_serials(store, exclude=None):
    """Normalised serials named on the shop's OPEN reservations (minus `exclude`) → {serial: reservation number}."""
    from inventory.serials import normalize as normalize_serial
    qs = ReservationItem.objects.filter(reservation__store=store, reservation__status=Reservation.Status.OPEN,
                                        reservation__is_deleted=False).exclude(serial_numbers=[])
    if exclude is not None:
        qs = qs.exclude(reservation=exclude)
    out = {}
    for item in qs.select_related('reservation'):
        for sn in item.serial_numbers or []:
            out[normalize_serial(sn)] = item.reservation.number
    return out


def _check_held_serials(invoice, items, own_reservation):
    from inventory.serials import normalize as normalize_serial
    held = held_serials(invoice.store, exclude=own_reservation)
    if not held:
        return
    taken = [{'serial': sn, 'reservation': held[normalize_serial(sn)]}
             for item in items for sn in (item.serial_numbers or []) if normalize_serial(sn) in held]
    if taken:
        raise ValidationError({'detail': 'A serial on this sale is held by a reservation.', 'code': 'serial_reserved',
                               'held': taken})


def post_sale(invoice, user, data, idem_key=''):
    """Returns the payment plan, or None when this exact checkout (same Idempotency-Key) already went through.
    Raises a 400 (ValidationError / CheckoutRefused / TreasuryRefused) — nothing is saved then."""
    from .views import require_open_shift
    items = list(invoice.items.select_related('variant__product').all())
    own_reservation = Reservation.all_objects.filter(sale=invoice).first()
    _check_held_serials(invoice, items, own_reservation)
    # Policy 0 — the shop sells only with an open shift (off by default). Before any payment / stock work.
    require_open_shift(invoice.store, user, invoice.branch)

    settings_obj = getattr(invoice.store, 'settings', None)

    # The till's payment comes WITH the checkout (s156, §AUDIT A2/A3) so the policies below judge the
    # sale as it will really be. s167: `payments` = several lines (cash + card + store credit + Ajel rest,
    # change on cash) — rules in finance/split_payment.py; the old single `method` still works.
    from .split_payment import plan_checkout_payments
    plan = plan_checkout_payments(invoice, data)
    paid_after = (invoice.paid_amount or Decimal('0')) + plan.paying

    # Policy 1 — credit (agel) selling. Block posting an unpaid invoice when
    # the owner disabled credit. No race here (reads invoice totals only).
    allow_agel = getattr(settings_obj, 'enable_agel_selling', True)
    if not allow_agel and (invoice.grand_total - paid_after) > 0:
        raise CheckoutRefused({'detail': 'Credit sales are disabled for this store. '
                                         'Collect full payment before completing the sale.'})

    # Policy 3 — credit limit. Now that this sale is about to extend credit
    # (if unpaid), enforce the store's ALLOW/WARN/BLOCK policy against the
    # customer's REAL outstanding balance. BLOCK raises → stays DRAFT.
    from .serializers import enforce_credit_policy
    enforce_credit_policy(invoice, paid_amount=paid_after)

    # Policy 4 — plan quota (s166). Only POSTED sales count, so this is the step that uses one up;
    # the check on draft-create alone let a shop open many carts at limit-1 and post them all.
    from billing.quota import enforce_quota
    enforce_quota(invoice.store, 'invoices')

    allow_negative = getattr(settings_obj, 'allow_negative_stock', False)

    with transaction.atomic():
        # s157 (§AUDIT A5): lock the sale and re-check it is still a DRAFT — two checkouts arriving together
        # (double click, retry) both passed the check above and both posted: stock out twice, paid twice.
        locked = SalesInvoice.objects.select_for_update().filter(pk=invoice.pk).values_list(
            'status', 'checkout_key').first()
        if locked and locked[0] != SalesInvoice.Status.DRAFT:
            if locked[0] == SalesInvoice.Status.POSTED and idem_key and locked[1] == idem_key:
                invoice.refresh_from_db()
                return None   # a retry of a sale that already went through
            raise CheckoutRefused({'detail': 'Invoice already posted.'})

        # Policy 2 — overselling. Lock the stock rows we're about to move and
        # verify availability INSIDE the transaction, so a concurrent checkout
        # can't slip between the check and the signal's decrement.
        if not allow_negative:
            shortages = []
            for item in items:
                # A "Service (No Stock)" product (e.g. repair labour, Upgrade 13) has no stock to run out of.
                if item.variant.product.product_type == item.variant.product.ProductType.SERVICE:
                    continue
                stock = (StockLevel.objects.select_for_update()
                         .filter(variant=item.variant, branch=invoice.branch).first())
                # s172: units held by OTHER open reservations are not for sale (a reservation's own sale is not
                # blocked by itself).
                available = (stock.quantity if stock else Decimal('0')) - held_qty(
                    item.variant, invoice.branch, exclude=own_reservation)
                # Compare in BASE units: a line of 1 Pack consumes factor base units.
                base_qty = Decimal(str(item.quantity)) * Decimal(str(item.unit_factor or 1))
                if base_qty > available:
                    shortages.append({
                        'variant': str(item.variant.id),
                        'sku': item.variant.sku,
                        'name': item.variant.product.name,
                        'requested': str(base_qty),
                        'available': str(available),
                    })
            if shortages:
                raise ValidationError({
                    'detail': 'Insufficient stock to complete this sale.',
                    'shortages': shortages,
                })

        # Policy 4 — expired stock (FEFO). When the store blocks expired sales,
        # peek at the batches this checkout would draw (earliest-expiry first) and
        # reject if covering the line forces a draw from an expired batch. WARN/
        # ALLOW fall through (the POS surfaces the warning pre-checkout).
        expired_policy = getattr(settings_obj, 'expired_sale_policy', 'WARN')
        if expired_policy == 'BLOCK':
            from inventory.models import is_expiry_tracked, StockBatch
            from django.db.models import F
            from django.utils import timezone as _tz
            today = _tz.now().date()
            blocked = []
            for item in items:
                if not is_expiry_tracked(item.variant):
                    continue
                need = Decimal(str(item.quantity)) * Decimal(str(item.unit_factor or 1))
                batches = (StockBatch.objects.select_for_update()
                           .filter(variant=item.variant, branch=invoice.branch,
                                   quantity_remaining__gt=0)
                           .order_by(F('expiry_date').asc(nulls_last=True), 'received_date'))
                for b in batches:
                    if need <= 0:
                        break
                    take = min(Decimal(str(b.quantity_remaining)), need)
                    if b.expiry_date and b.expiry_date < today:
                        blocked.append({
                            'variant': str(item.variant.id),
                            'sku': item.variant.sku,
                            'name': item.variant.product.name,
                            'expiry_date': b.expiry_date.isoformat(),
                        })
                        break
                    need -= take
            if blocked:
                raise ValidationError({
                    'detail': 'This sale would draw from expired stock, which this store blocks.',
                    'expired': blocked,
                })

        # Flip to POSTED → fires handle_sale_stock (decrement + COGS snapshot)
        # and assigns the human-readable invoice_number.
        invoice.status = SalesInvoice.Status.POSTED
        invoice.checkout_key = idem_key
        invoice.change_given = plan.change
        invoice.save()
        if plan.store_credit > 0:
            # s167: spend the wallet with the customer row LOCKED, re-checked here — two tills spending the
            # same credit at once can't both succeed. Raising rolls the whole sale back to DRAFT.
            from users.models import Customer
            cust = Customer.all_objects.select_for_update().get(pk=invoice.customer_id)
            if plan.store_credit > (cust.store_credit or Decimal('0')):
                raise ValidationError({'detail': f'{cust.name} has only {cust.store_credit:.2f} store credit.'})
            cust.store_credit = cust.store_credit - plan.store_credit
            cust.save(update_fields=['store_credit'])
        from .treasury_flows import post_sale_payment
        for pay_method, amount in plan.lines:
            pay = Payment.objects.create(invoice=invoice, method=pay_method, amount=amount, created_by=user)
            post_sale_payment(pay)   # TREASURY.md: + the box this money lands in (no-op with the Treasury off)
        if plan.lines:
            invoice.refresh_from_db()
    return plan
