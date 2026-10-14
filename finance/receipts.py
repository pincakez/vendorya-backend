"""Receive a customer's debt payment as ONE lump sum (s168, PROLINE-PARITY Upgrade 2, Proline "دفع عملاء").

The money is spread oldest-first over the customer's unpaid POSTED sales as ordinary Payment rows (so each
sale's paid_amount moves through Payment.save like any other payment); what is left pays the opening balance;
anything beyond everything owed is an advance (credit we owe them) — refused unless the caller says so.
"""
from decimal import Decimal

from django.db import transaction
from rest_framework.exceptions import ValidationError

from users.models import Customer
from .models import (
    CustomerReceipt, Payment, SalesInvoice,
    customer_outstanding, invoice_outstanding, with_refund_totals,
)

ZERO = Decimal('0')
CENT = Decimal('0.01')


def _q(d):
    return str(Decimal(d).quantize(CENT))


def receive_customer_payment(*, store, customer, amount, method, user, date=None, note='', allow_advance=False):
    """Create the receipt + its Payments atomically, with the customer row locked. Returns (receipt, summary)."""
    amount = Decimal(amount).quantize(CENT)
    with transaction.atomic():
        customer = Customer.all_objects.select_for_update().get(pk=customer.pk, store=store)
        posted = SalesInvoice.all_objects.filter(
            store=store, customer=customer, status=SalesInvoice.Status.POSTED, is_deleted=False)
        # Lock the sales too, so a payment taken on one of them right now can't overpay it.
        inv_ids = list(posted.select_for_update().order_by().values_list('id', flat=True))

        owed = customer_outstanding(customer)
        beyond = max(ZERO, amount - max(owed, ZERO))
        if beyond > 0 and not allow_advance:
            raise ValidationError({'amount': (
                f'More than this customer owes ({_q(max(owed, ZERO))}). '
                f'To keep the extra {_q(beyond)} as credit for them, tick "keep the extra as credit".')})

        receipt = CustomerReceipt.objects.create(
            store=store, customer=customer, amount=amount, method=method,
            note=note or '', created_by=user, **({'date': date} if date else {}))

        left, allocations = amount, []
        invoices = (with_refund_totals(SalesInvoice.all_objects.filter(id__in=inv_ids))
                    .order_by('date', 'invoice_number', 'created_at'))
        for inv in invoices:                       # oldest sale first
            if left <= 0:
                break
            due = invoice_outstanding(inv)
            if due <= 0:
                continue
            part = min(due, left)
            Payment.objects.create(invoice=inv, method=method, amount=part, created_by=user, receipt=receipt)
            left -= part
            allocations.append({'invoice': str(inv.id), 'invoice_number': inv.invoice_number,
                                'amount': _q(part), 'still_owed': _q(due - part)})

        receipt.unallocated = left
        receipt.advance = beyond
        receipt.save(update_fields=['unallocated', 'advance', 'updated_at'])

        summary = {
            'allocations': allocations,
            'to_invoices': _q(amount - left),
            'to_opening_balance': _q(max(ZERO, left - beyond)),
            'advance': _q(beyond),
            'balance_before': _q(owed),
            'balance_after': _q(customer_outstanding(customer)),
        }
    return receipt, summary


def reverse_customer_receipt(receipt):
    """Soft-delete a receipt AND its Payments — each sale's paid_amount is rebuilt by Payment.save,
    and the unallocated part drops out of customer_outstanding with the receipt itself."""
    with transaction.atomic():
        Customer.all_objects.select_for_update().get(pk=receipt.customer_id)
        for p in Payment.all_objects.filter(receipt=receipt, is_deleted=False).select_related('invoice'):
            p.delete()
        receipt.delete()
