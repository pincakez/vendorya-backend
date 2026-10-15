"""Service desk money + stock (PROLINE-PARITY Upgrade 13).

Nothing here writes stock or money by hand. A finished job becomes a normal DRAFT sale that is completed by the
till's own checkout (finance.views.SalesInvoiceViewSet.checkout — payment plan, split payment, Ajel rules, credit
policy, invoice quota, oversell check, FEFO, COGS); a returned job goes back through the normal return
(finance.serializers.RefundInvoiceSerializer — paid-back cap, drawer cash, store credit, restock). Prices + tax
come from the ONE sale rule (SalesInvoiceSerializer.price_lines), for the quote and the invoice alike.
"""
from decimal import Decimal, ROUND_DOWN

from django.db import IntegrityError, transaction

ZERO = Decimal('0')
CENT = Decimal('0.01')

LABOUR_NAME = {'en': 'Repair service', 'ar': 'خدمة صيانة'}


def find_labour_variant(store):
    """The shop's system "Repair service" variant if it exists (never creates anything)."""
    from inventory.models import ProductVariant
    from .models import ServiceDeskSettings
    vid = (ServiceDeskSettings.objects.filter(store=store).values_list('labour_variant_id', flat=True).first())
    if not vid:
        return None
    return ProductVariant.all_objects.select_related('product__tax').filter(pk=vid, store=store).first()


def labour_variant(store):
    """The shop's ONE system "Repair service" variant — made on first use, revived if it was deleted.

    A SERVICE-type product with no supplier: no stock (the till's oversell check skips it, it never gets a
    stock row), no SKU (ProductVariant.save skips SKU for it — nothing is scanned), hidden from the POS. The
    owner may rename it and give it its own tax (PATCH /api/services/labour/); without one, the shop's default
    tax applies — the same rule as any sale line.
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
            variant.restore()
        return variant

    lang = 'ar' if (getattr(store, 'default_language', '') or '').startswith('ar') else 'en'
    product = Product.all_objects.create(
        store=store, name=LABOUR_NAME[lang],
        product_type=Product.ProductType.SERVICE,
        hide_from_pos=True,
    )
    variant = ProductVariant.all_objects.create(product=product, sell_price=ZERO, cost_price=ZERO)
    cfg.labour_variant = variant
    cfg.save(update_fields=['labour_variant'])
    return variant


def job_lines(service, labour=None):
    """What the job bills, as sale lines [(variant, quantity, unit_price, name, serials)]: one labour line
    (detection fee + repair charge) + one line per part (a serial-tracked part carries its unit serials — two
    parts of the same product stay two lines, each with its own). A warranty job charges 0 for everything (the parts
    still leave stock). `labour` = the labour variant (None for a quote before the shop's first Done: the
    line is still taxed right — a fresh labour product has no own tax, so the shop default applies)."""
    free = service.is_warranty
    parts = [p for p in service.parts.all() if not p.is_deleted]
    lines = []
    amount = ZERO if free else service.labour_total
    if amount > 0 or not parts:   # a 0-labour job with parts needs no empty labour line on the receipt
        name = labour.product.name if labour is not None else LABOUR_NAME[
            'ar' if (getattr(service.store, 'default_language', '') or '').startswith('ar') else 'en']
        lines.append((labour, Decimal('1'), amount, name, []))
    for p in parts:
        lines.append((p.variant, p.quantity, ZERO if free else p.unit_price, p.variant.product.name,
                      list(p.serial_numbers or [])))
    return lines


def quote(service):
    """The job's invoice as Done will make it — lines, tax, grand total — computed, never saved."""
    from finance.models import SalesInvoice
    from finance.serializers import SalesInvoiceSerializer
    lines = job_lines(service, labour=find_labour_variant(service.store))
    priced = SalesInvoiceSerializer.price_lines(
        SalesInvoice(store=service.store, discount=ZERO), [(v, q, p, ZERO) for v, q, p, _n, _s in lines])
    return {
        'lines': [{
            'name': name, 'quantity': str(q), 'unit_price': str(p), 'serial_numbers': serials,
            'tax_rate': str(pl['tax_rate']), 'tax_amount': str(pl['tax_amount']), 'total': str(pl['total']),
        } for (_v, q, p, name, serials), pl in zip(lines, priced['lines'])],
        'subtotal': str(priced['subtotal']),
        'tax_total': str(priced['tax_total']),
        'grand_total': str(priced['grand_total']),
    }


def job_parts_branch(service):
    """The branch the job's serial-tracked parts sit in (IN_STOCK units), or None when it has none. Done sells from
    it, so whoever clicks Done, the units are found where they are. The job form keeps them in ONE branch."""
    from inventory.models import SerialUnit
    serials = [s for p in service.parts.filter(is_deleted=False) for s in (p.serial_numbers or [])]
    if not serials:
        return None
    unit = (SerialUnit.all_objects.filter(store=service.store, serial__in=serials, status=SerialUnit.Status.IN_STOCK,
                                          is_deleted=False).select_related('branch').first())
    return unit.branch if unit else None


def add_job_lines(service, invoice):
    """Put the job on its DRAFT sale (the caller then prices it with the normal sale rule). A tracked part's
    serials ride on its line, so the posting (inventory.serials.post_sale) sells exactly those units."""
    from finance.models import SalesInvoiceItem
    for variant, qty, price, _name, serials in job_lines(service, labour=labour_variant(service.store)):
        SalesInvoiceItem.objects.create(invoice=invoice, variant=variant, quantity=qty, unit_price=price,
                                        serial_numbers=serials)


def refund_lines(invoice, restock_parts=True):
    """Everything still returnable on the job's sale, one line per variant, in the product's BASE unit (no
    `unit` sent = base unit): the quantity + money left after earlier returns — counted by the same helper
    the return cap uses (finance.models.returned_so_far), so the two can never disagree. The labour line is
    never restocked (a service has no stock); parts are, unless `restock_parts` is False (a dead part). A
    serial-tracked part goes back with the serials of its units still SOLD on this sale (inventory.serials
    .return_units puts them back on the shelf, or keeps them apart for a dead part)."""
    from finance.models import returned_so_far
    from inventory.models import ProductVariant, Product, SerialUnit

    counts = returned_so_far(invoice)
    sold_serials = {}
    for vid, serial in (SerialUnit.all_objects.filter(store=invoice.store, sale_item__invoice=invoice,
                                                      status=SerialUnit.Status.SOLD, is_deleted=False)
                        .order_by('serial').values_list('variant_id', 'serial')):
        sold_serials.setdefault(vid, []).append(serial)
    services = set(ProductVariant.all_objects.filter(
        pk__in=list(counts), product__product_type=Product.ProductType.SERVICE).values_list('pk', flat=True))
    lines = []
    for vid, r in counts.items():
        # A refund line holds 3 decimals; round DOWN so the line never asks for more than is left.
        q_left = (r['sold'] - r['returned']).quantize(Decimal('0.001'), rounding=ROUND_DOWN)
        if q_left <= 0 or r['sold'] <= 0:
            continue
        per_base = r['sold_value'] / r['sold']
        money = max(min(per_base * q_left, r['sold_value'] - r['returned_value']), ZERO).quantize(CENT)
        lines.append({
            'variant': str(vid),
            'quantity': str(q_left),
            'refund_amount': str(money),
            'restock_inventory': bool(restock_parts) and vid not in services,
            'serial_numbers': sold_serials.get(vid, []),
        })
    return lines
