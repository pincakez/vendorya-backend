"""Service desk money + stock (PROLINE-PARITY Upgrade 13).

Nothing here writes stock or money by hand. A finished job becomes a normal DRAFT sale that is completed by the
till's own checkout (finance.views.SalesInvoiceViewSet.checkout — payment plan, split payment, Ajel rules, credit
policy, invoice quota, oversell check, FEFO, COGS); a returned job goes back through the normal return
(finance.serializers.RefundInvoiceSerializer — paid-back cap, drawer cash, store credit, restock).
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum

ZERO = Decimal('0')
CENT = Decimal('0.01')

LABOUR_NAME = {'en': 'Repair service', 'ar': 'خدمة صيانة'}


def labour_variant(store):
    """The shop's ONE system "Repair service" variant — made on first use, revived if it was deleted.

    A SERVICE-type product (no stock: the till's oversell check skips it and it never gets a stock row), hidden
    from the POS, with no SKU (it is never scanned or sold by hand). Its own tax (if the owner sets one on the
    product) applies to repair labour; otherwise the shop's default tax — the same rule as any sale line.
    """
    from inventory.models import Product, ProductVariant
    from .models import ServiceDeskSettings

    try:
        with transaction.atomic():
            cfg, _ = ServiceDeskSettings.objects.get_or_create(store=store)
    except IntegrityError:   # another request made it a moment ago
        cfg = ServiceDeskSettings.objects.get(store=store)
    cfg = ServiceDeskSettings.objects.select_for_update().get(pk=cfg.pk)   # one creator at a time

    variant = (ProductVariant.all_objects.select_related('product')
               .filter(pk=cfg.labour_variant_id, store=store).first()) if cfg.labour_variant_id else None
    if variant is not None:
        if variant.product.is_deleted:
            variant.product.restore()
        if variant.is_deleted:
            # .update(): ProductVariant.save() would try to give this SKU-less row a supplier SKU.
            ProductVariant.all_objects.filter(pk=variant.pk).update(is_deleted=False, deleted_at=None)
            variant.is_deleted = False
        return variant

    lang = 'ar' if (getattr(store, 'default_language', '') or '').startswith('ar') else 'en'
    product = Product.all_objects.create(
        store=store, name=LABOUR_NAME[lang],
        product_type=Product.ProductType.SERVICE,
        hide_from_pos=True,
    )
    # bulk_create skips ProductVariant.save(), which would demand a supplier to build a SKU. sku stays NULL
    # (allowed: NULLs never collide under the per-shop SKU constraint), so nobody can scan it at the till.
    variant = ProductVariant(product=product, store=store, sku=None, sell_price=ZERO, cost_price=ZERO)
    ProductVariant.all_objects.bulk_create([variant])
    cfg.labour_variant = variant
    cfg.save(update_fields=['labour_variant'])
    return variant


def add_job_lines(service, invoice):
    """Put the job on its DRAFT sale: one labour line (detection fee + repair charge) + one line per part.
    A warranty job charges 0 for everything (the parts still leave stock at checkout). Prices are before tax;
    the caller recalculates tax + totals with the normal sale rule."""
    from finance.models import SalesInvoiceItem

    free = service.is_warranty
    parts = list(service.parts.filter(is_deleted=False).select_related('variant'))
    labour = ZERO if free else service.labour_total
    if labour > 0 or not parts:   # a 0-labour job with parts needs no empty labour line on the receipt
        SalesInvoiceItem.objects.create(
            invoice=invoice, variant=labour_variant(service.store),
            quantity=Decimal('1'), unit_price=labour,
        )
    for p in parts:
        SalesInvoiceItem.objects.create(
            invoice=invoice, variant=p.variant,
            quantity=p.quantity, unit_price=ZERO if free else p.unit_price,
        )


def refund_lines(invoice, restock_parts=True):
    """What is still returnable on the job's sale, per variant: quantity + money not yet refunded (earlier
    returns on the same sale are subtracted, using the same caps the normal return checks). The labour line is
    never restocked (a service has no stock); parts are, unless `restock_parts` is False (a dead part)."""
    from finance.models import RefundItem

    sold = {}
    for it in invoice.items.select_related('variant__product'):
        row = sold.setdefault(it.variant_id, {'variant': it.variant, 'qty': ZERO, 'value': ZERO})
        row['qty'] += it.quantity
        row['value'] += it.total or ZERO
    done = {r['variant_id']: r for r in (
        RefundItem.objects.filter(refund__original_invoice=invoice, refund__is_deleted=False)
        .values('variant_id').annotate(q=Sum('quantity'), v=Sum('refund_amount')))}

    lines = []
    for vid, row in sold.items():
        q_left = row['qty'] - (done.get(vid, {}).get('q') or ZERO)
        if q_left <= 0:
            continue
        v_left = row['value'] - (done.get(vid, {}).get('v') or ZERO)
        per_unit = row['value'] / row['qty'] if row['qty'] else ZERO
        money = max(min(per_unit * q_left, v_left), ZERO).quantize(CENT)
        is_service = row['variant'].product.product_type == row['variant'].product.ProductType.SERVICE
        lines.append({
            'variant': str(vid),
            'quantity': str(q_left),
            'refund_amount': str(money),
            'restock_inventory': bool(restock_parts) and not is_service,
        })
    return lines
