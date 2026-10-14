"""Split payment at the till (s167, PROLINE-PARITY Upgrades 1 + 5): several payment lines in ONE checkout —
cash + card + store credit + the rest on Ajel, change on the cash line. Every call here is the CASHIER's."""
from decimal import Decimal

from finance.models import (
    Payment, PaymentMethod, RefundInvoice, SalesInvoice, WorkShift, customer_outstanding,
)
from finance.tests import _TillBase
from users.models import User


class _SplitBase(_TillBase):

    def setUp(self):
        super().setUp()
        self.card = PaymentMethod.objects.create(store=self.store, name='Visa')
        self.inv = self._big_draft()

    def _big_draft(self, customer=None):
        inv = self._draft(customer)
        inv.items.update(unit_price=Decimal('1000'), total=Decimal('1000'))
        SalesInvoice.objects.filter(pk=inv.pk).update(grand_total=Decimal('1000'))
        inv.refresh_from_db()
        return inv

    def _pay(self, lines, inv=None):
        inv = inv or self.inv
        body = {'payments': [{'method': m if isinstance(m, str) else str(m.id), 'amount': a} for m, a in lines]}
        return self.client.post(self.URL.format(inv.id), body, format='json')

    def _wallet(self, amount=None):
        if amount is not None:
            self.customer.store_credit = Decimal(amount)
            self.customer.save(update_fields=['store_credit'])
        self.customer.refresh_from_db()
        return self.customer.store_credit

    def _assert_still_draft(self, r, inv=None):
        inv = inv or self.inv
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIsInstance(r.data['detail'], str)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SalesInvoice.Status.DRAFT)
        self.assertEqual(Payment.objects.filter(invoice=inv).count(), 0)



class SplitPaymentCheckoutTests(_SplitBase):

    # ── the asked-for cases ────────────────────────────────────────────────────────────────────────────
    def test_cash_600_plus_card_400(self):
        r = self.client.post('/api/finance/shifts/', {'branch': str(self.branch.id), 'starting_cash': '50'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        shift_id = r.data['id']
        r = self._pay([(self.cash, '600'), (self.card, '400')])
        self.assertEqual(r.status_code, 200, r.content)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, SalesInvoice.Status.POSTED)
        self.assertEqual(self.inv.paid_amount, Decimal('1000'))
        self.assertEqual(self.inv.change_given, Decimal('0'))
        pays = {p.method.name: p.amount for p in Payment.objects.filter(invoice=self.inv)}
        self.assertEqual(pays, {'Cash': Decimal('600'), 'Visa': Decimal('400')})
        d = self.client.get('/api/finance/shifts/drawer/').data
        self.assertEqual(Decimal(d['cash_in']), Decimal('600'))           # the card part is not drawer cash
        self.assertEqual(Decimal(d['expected_balance']), Decimal('650'))
        self.assertEqual(len(d['moves']), 1)
        c = self.client.post(f'/api/finance/shifts/{shift_id}/close/', {'counted_cash': '650'}, format='json')
        self.assertEqual(Decimal(c.data['expected_cash']), Decimal('650'))
        self.assertEqual(Decimal(c.data['difference']), Decimal('0'))

    def test_cash_1200_gives_200_change(self):
        r = self._pay([(self.cash, '1200')])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Decimal(r.data['change_given']), Decimal('200'))
        p = Payment.objects.get(invoice=self.inv)
        self.assertEqual(p.amount, Decimal('1000'))                      # the Payment = money KEPT
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.paid_amount, self.inv.change_given), (Decimal('1000'), Decimal('200')))

    def test_card_overpay_refused(self):
        self._assert_still_draft(self._pay([(self.card, '1200')]))

    def test_cash_300_rest_on_ajel(self):
        r = self._pay([(self.cash, '300'), (self.agel, '700')])
        self.assertEqual(r.status_code, 200, r.content)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.paid_amount, Decimal('300'))
        self.assertEqual(customer_outstanding(self.customer), Decimal('700'))
        self.assertEqual(Payment.objects.filter(invoice=self.inv).count(), 1)   # Ajel is not a payment

    def test_ajel_rest_blocked_over_credit_limit(self):
        self.settings.credit_policy = 'BLOCK'
        self.settings.save()
        self.customer.credit_limit = Decimal('500')
        self.customer.save()
        r = self._pay([(self.cash, '300'), (self.agel, '0')])      # 700 on account > 500 limit
        self.assertEqual(r.status_code, 400, r.content)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, SalesInvoice.Status.DRAFT)
        r = self._pay([(self.cash, '500'), (self.agel, '0')])      # 500 on account = the limit → fine
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(customer_outstanding(self.customer), Decimal('500'))

    def test_ajel_on_walkin_refused(self):
        inv = self._big_draft(customer=self.walkin)
        self._assert_still_draft(self._pay([(self.cash, '300'), (self.agel, '700')], inv), inv)

    def test_ajel_rest_refused_when_credit_selling_is_off(self):
        self.settings.enable_agel_selling = False
        self.settings.save()
        self._assert_still_draft(self._pay([(self.cash, '300'), (self.agel, '700')]))

    def test_store_credit_150_of_200(self):
        self._wallet('200')
        r = self._pay([('STORE_CREDIT', '150'), (self.cash, '850')])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self._wallet(), Decimal('50'))
        sc = Payment.objects.get(invoice=self.inv, method__is_store_credit=True)
        self.assertEqual(sc.amount, Decimal('150'))
        self.assertFalse(sc.method.is_cash)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.paid_amount, Decimal('1000'))

    def test_store_credit_over_available_refused(self):
        self._wallet('200')
        self._assert_still_draft(self._pay([('STORE_CREDIT', '250'), (self.cash, '750')]))
        self.assertEqual(self._wallet(), Decimal('200'))

    def test_store_credit_needs_named_customer(self):
        self.walkin.store_credit = Decimal('500')
        self.walkin.save()
        inv = self._big_draft(customer=self.walkin)
        self._assert_still_draft(self._pay([('STORE_CREDIT', '100'), (self.cash, '900')], inv), inv)

    def test_store_credit_is_not_drawer_cash(self):
        self.client.post('/api/finance/shifts/', {'branch': str(self.branch.id), 'starting_cash': '0'}, format='json')
        self._wallet('1000')
        self.assertEqual(self._pay([('STORE_CREDIT', '1000')]).status_code, 200)
        self.assertEqual(Decimal(self.client.get('/api/finance/shifts/drawer/').data['cash_in']), Decimal('0'))

    def test_old_single_method_request_still_works(self):
        r = self._checkout(self.inv, self.cash)
        self.assertEqual(r.status_code, 200, r.content)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.paid_amount, Decimal('1000'))
        inv2 = self._big_draft()
        self.assertEqual(self._checkout(inv2, self.agel).status_code, 200)
        self.assertEqual(customer_outstanding(self.customer), Decimal('1000'))

    def test_another_shops_method_refused(self):
        from core.models import Store
        other_owner = User.objects.create_user(username='own_o', password='x', role='OWNER')
        other = Store.objects.create(name='O', store_code='102', owner=other_owner)
        foreign = PaymentMethod.objects.create(store=other, name='Cash', is_cash=True)
        self._assert_still_draft(self._pay([(foreign, '1000')]))
        self._assert_still_draft(self._pay([('not-a-uuid', '1000')]))

    # ── the rules around it ────────────────────────────────────────────────────────────────────────────
    def test_unpaid_rest_without_ajel_refused(self):
        self._assert_still_draft(self._pay([(self.cash, '300')]))

    def test_zero_negative_and_bad_amounts_refused(self):
        for bad in ('0', '-5', 'abc', '10.005', None):
            self._assert_still_draft(self._pay([(self.cash, bad), (self.card, '1000')]))
        self._assert_still_draft(self.client.post(self.URL.format(self.inv.id), {'payments': []}, format='json'))

    def test_same_method_twice_is_merged(self):
        r = self._pay([(self.cash, '500'), (self.cash, '500')])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(list(Payment.objects.filter(invoice=self.inv).values_list('amount', flat=True)),
                         [Decimal('1000')])

    def test_change_comes_off_the_cash_line_only(self):
        r = self._pay([(self.card, '400'), (self.cash, '700')])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Decimal(r.data['change_given']), Decimal('100'))
        pays = {p.method.name: p.amount for p in Payment.objects.filter(invoice=self.inv)}
        self.assertEqual(pays, {'Visa': Decimal('400'), 'Cash': Decimal('600')})

    def test_card_plus_store_credit_over_due_refused(self):
        self._wallet('500')
        self._assert_still_draft(self._pay([(self.card, '900'), ('STORE_CREDIT', '200')]))

    def test_store_credit_method_hidden_and_not_payable_later(self):
        self._wallet('300')
        self._pay([('STORE_CREDIT', '300'), (self.agel, '700')])
        data = self.client.get('/api/finance/payment-methods/').data
        ids = [m['id'] for m in (data['results'] if isinstance(data, dict) else data)]
        self.assertIn(str(self.cash.id), [str(i) for i in ids])
        sc = PaymentMethod.objects.get(store=self.store, is_store_credit=True)
        self.assertNotIn(str(sc.id), [str(i) for i in ids])
        r = self.client.post('/api/finance/payments/', {'invoice': str(self.inv.id), 'method': str(sc.id),
                                                        'amount': '100'}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(self._wallet(), Decimal('0'))

    def test_receipt_lists_every_line_and_change(self):
        self._wallet('100')
        self.assertEqual(self._pay([('STORE_CREDIT', '100'), (self.card, '400'), (self.cash, '600')]).status_code, 200)
        d = self.client.get(f'/api/finance/invoices/{self.inv.id}/print-data/').data
        self.assertEqual(Decimal(d['invoice']['change_given']), Decimal('100'))
        lines = {(p['method'], p['is_cash'], p['is_store_credit']): Decimal(p['amount']) for p in d['payments']}
        self.assertEqual(lines, {('Store credit', False, True): Decimal('100'), ('Visa', False, False): Decimal('400'),
                                 ('Cash', True, False): Decimal('500')})


class SplitPaymentRefundTests(_SplitBase):
    """Returning a split sale: the store-credit share goes back to the WALLET first (never turned into cash),
    then cash up to the cash kept, the rest back to the card."""

    def setUp(self):
        super().setUp()
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken
        self.manager = User.objects.create_user(username='mgr_sp', password='x', role='MANAGER', store=self.store)
        self.mgr = APIClient()
        self.mgr.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.manager).access_token))

    def _refund(self, amount, method='CASH'):
        r = self.mgr.post('/api/finance/refunds/', {
            'branch': str(self.branch.id), 'original_invoice': str(self.inv.id), 'customer': str(self.customer.id),
            'refund_method': method,
            'items': [{'variant': str(self.variant.id), 'quantity': '1', 'refund_amount': amount}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return RefundInvoice.objects.get(pk=r.data['id'])

    def test_full_return_of_store_credit_plus_cash_sale(self):
        self._wallet('400')
        self.assertEqual(self._pay([('STORE_CREDIT', '400'), (self.cash, '600')]).status_code, 200)
        self.assertEqual(self._wallet(), Decimal('0'))
        ref = self._refund('1000')
        self.assertEqual((ref.paid_back, ref.store_credit_back, ref.drawer_cash),
                         (Decimal('1000'), Decimal('400'), Decimal('600')))
        self.assertEqual(self._wallet(), Decimal('400'))
        self.assertEqual(customer_outstanding(self.customer), Decimal('0'))

    def test_partial_return_fills_the_wallet_first(self):
        self._wallet('400')
        self._pay([('STORE_CREDIT', '400'), (self.card, '600')])
        ref = self._refund('500')
        self.assertEqual((ref.store_credit_back, ref.drawer_cash), (Decimal('400'), Decimal('0')))   # 100 → card
        self.assertEqual(self._wallet(), Decimal('400'))

    def test_change_sale_refund_pays_back_only_cash_kept(self):
        self._pay([(self.cash, '1200')])
        ref = self._refund('1000')
        self.assertEqual((ref.drawer_cash, ref.store_credit_back), (Decimal('1000'), Decimal('0')))
