"""Cheques (PROLINE-PARITY "cheques with due dates") — the ONE door for every state change, each atomic.

PENDING ──clear──▶ CLEARED ──bounce──▶ BOUNCED      PENDING ──bounce──▶ BOUNCED      PENDING ──cancel──▶ CANCELLED

No new money logic: clearing a RECEIVED cheque is a normal customer payment (`receive_customer_payment`, extra kept
as credit), clearing a GIVEN cheque is a normal supplier payment (+ its box line). A bounce after clearing reverses
that payment exactly like deleting it by hand — the debt comes back. A pending cheque moves nothing.
"""
from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Cheque, PaymentMethod, SupplierPayment

CENT = Decimal('0.01')


def _err(code, detail, **extra):
    return ValidationError({'code': code, 'detail': detail, **{k: str(v) for k, v in extra.items()}})


def _check_method(store, method):
    if method is None:
        raise _err('method_required', 'Choose how the cheque is paid (e.g. a "Cheque" method that lands in the bank).')
    if method.store_id != store.id or method.is_deleted:
        raise _err('method_not_found', 'Payment method not found.')
    if method.is_cash or method.is_agel or method.is_store_credit:
        raise _err('method_not_cheque', 'A cheque is paid through a bank / cheque method — not cash, Ajel or store credit.')


@transaction.atomic
def create_cheque(store, user, *, direction, customer=None, supplier=None, number='', bank_name='', amount=None,
                  issue_date=None, due_date=None, method=None, note=''):
    if direction not in Cheque.Direction.values:
        raise _err('bad_direction', 'Choose received or given.')
    if direction == Cheque.Direction.RECEIVED:
        if customer is None or customer.store_id != store.id or customer.is_deleted or customer.is_walk_in:
            raise _err('customer_required', 'Choose the customer who gave the cheque.')
        supplier = None
    else:
        if supplier is None or supplier.store_id != store.id or supplier.is_deleted:
            raise _err('supplier_required', 'Choose the supplier the cheque is for.')
        customer = None
    number = (number or '').strip()
    if not number:
        raise _err('number_required', 'Write the cheque number.')
    try:
        amount = Decimal(str(amount)).quantize(CENT)
    except (InvalidOperation, TypeError, ValueError):
        raise _err('bad_amount', 'Write the amount.')
    if not amount.is_finite() or amount <= 0:
        raise _err('bad_amount', 'The amount must be above zero.')
    if due_date is None:
        raise _err('due_required', 'Write the due date.')
    if method is not None:
        _check_method(store, method)
    return Cheque.objects.create(
        store=store, direction=direction, customer=customer, supplier=supplier, number=number,
        bank_name=(bank_name or '').strip(), amount=amount, issue_date=issue_date or timezone.localdate(),
        due_date=due_date, method=method, note=note or '', created_by=user)


def _lock(cheque):
    return Cheque.all_objects.select_for_update().get(pk=cheque.pk)


@transaction.atomic
def clear_cheque(cheque, user, *, method=None, box=None, on=None):
    """The money really arrived (RECEIVED) / left (GIVEN)."""
    from .receipts import receive_customer_payment
    from .supplier_payment_methods import legacy_code_for
    from .treasury_flows import check_box, guarded, sync_paid_out
    from treasury import ledger
    from treasury.models import MoneyMove
    cheque = _lock(cheque)
    if cheque.status != Cheque.Status.PENDING:
        raise _err('not_pending', 'Only a waiting cheque can be cleared.')
    store = cheque.store
    method = method or cheque.method
    _check_method(store, method)
    on = on or timezone.localdate()
    when = timezone.now() if on == timezone.localdate() else timezone.make_aware(datetime.combine(on, time(12)))
    note = f'Cheque {cheque.number}' + (f' · {cheque.bank_name}' if cheque.bank_name else '')
    if cheque.direction == Cheque.Direction.RECEIVED:
        receipt, _ = receive_customer_payment(store=store, customer=cheque.customer, amount=cheque.amount,
                                              method=method, user=user, date=when, note=note, allow_advance=True)
        cheque.customer_receipt = receipt
    else:
        with guarded():
            if ledger.is_on(store):
                box = box or getattr(method, 'lands_in', None)
                if box is None:
                    raise _err('box_required', 'Choose the box (bank account) the cheque was paid from.')
                check_box(box, store, 'box')
            else:
                box = None
            pay = SupplierPayment.objects.create(store=store, supplier=cheque.supplier, amount=cheque.amount,
                                                 date=when, payment_method=method, method=legacy_code_for(method),
                                                 paid_from=box, note=note, created_by=user)
            sync_paid_out(pay, kind=MoneyMove.Kind.SUPPLIER_OUT, link='supplier_payment', user=user, created=True,
                          reason_edit='Cheque payment edited', reason_delete='Cheque bounced')
        cheque.supplier_payment = pay
    cheque.status = Cheque.Status.CLEARED
    cheque.method = method
    cheque.cleared_on = on
    cheque.save(update_fields=['status', 'method', 'cleared_on', 'customer_receipt', 'supplier_payment', 'updated_at'])
    return cheque


@transaction.atomic
def bounce_cheque(cheque, user, reason):
    """The bank refused it. After clearing, the payment it made is reversed (the debt comes back)."""
    from .receipts import reverse_customer_receipt
    from .treasury_flows import guarded, sync_paid_out
    from treasury.models import MoneyMove
    if not (reason or '').strip():
        raise _err('reason_required', 'Write why the cheque bounced.')
    cheque = _lock(cheque)
    if cheque.status not in (Cheque.Status.PENDING, Cheque.Status.CLEARED):
        raise _err('not_open', 'This cheque is already closed.')
    if cheque.status == Cheque.Status.CLEARED:
        if cheque.customer_receipt_id:
            reverse_customer_receipt(cheque.customer_receipt, user)
        if cheque.supplier_payment_id:
            pay = cheque.supplier_payment
            pay.delete()
            with guarded():
                sync_paid_out(pay, kind=MoneyMove.Kind.SUPPLIER_OUT, link='supplier_payment', user=user,
                              reason_delete=f'Cheque {cheque.number} bounced')
    cheque.status = Cheque.Status.BOUNCED
    cheque.status_reason = reason.strip()
    cheque.save(update_fields=['status', 'status_reason', 'updated_at'])
    return cheque


@transaction.atomic
def cancel_cheque(cheque, user, reason):
    """Typed by mistake / given back unused — only while it is still waiting."""
    if not (reason or '').strip():
        raise _err('reason_required', 'Write why the cheque is cancelled.')
    cheque = _lock(cheque)
    if cheque.status != Cheque.Status.PENDING:
        raise _err('not_pending', 'Only a waiting cheque can be cancelled.')
    cheque.status = Cheque.Status.CANCELLED
    cheque.status_reason = reason.strip()
    cheque.save(update_fields=['status', 'status_reason', 'updated_at'])
    return cheque
