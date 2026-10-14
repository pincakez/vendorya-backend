from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from core.models import Store
from finance.models import (CustomerReceipt, Payment, PaymentMethod, RefundInvoice, SalesInvoice,
                            customer_outstanding)
from finance.tests import _TillBase
from finance.tests_customer_receipt import _client
from inventory.models import StockLevel
from users.models import Customer, User

URL = '/api/auth/customers/{}/statement/'
D = Decimal


class CustomerStatementTests(_TillBase):
    """s167, PROLINE-PARITY Upgrade 3 (customer half): the customer account statement (كشف حساب).
    Receivable side: debit = they owe more, credit = they owe less, balance = debit − credit. Hard rule:
    with no dates the closing balance == customer_outstanding() to the cent."""

    def setUp(self):
        super().setUp()
        StockLevel.objects.filter(variant=self.variant).update(quantity=D('1000'))
        self.settings.restocking_fee_percent = D('10')
        self.settings.save()
        self.manager = User.objects.create_user(username='mgr_st', password='x', role='MANAGER', store=self.store)
        self.mgr = _client(self.manager)
        self.now = timezone.now()

    def _ago(self, days):
        return self.now - timedelta(days=days)

    def _day(self, days):
        return timezone.localtime(self._ago(days)).date().isoformat()

    def _sale(self, method):
        inv = self._draft()
        self.assertEqual(self._checkout(inv, method).status_code, 200)
        inv.refresh_from_db()
        return inv

    def _move(self, inv, days):
        """Back-date a sale and the payments taken on it."""
        SalesInvoice.all_objects.filter(pk=inv.pk).update(date=self._ago(days), created_at=self._ago(days))
        Payment.all_objects.filter(invoice=inv, receipt__isnull=True).update(created_at=self._ago(days))

    def _history(self):
        """Opening 500 · cash sale 100 · two Ajel sales 100 + 100 · a payment later deleted · a 250 lump
        receipt (100 + 100 to the sales, 50 to the opening balance) · a cash return of 100 with a 10 fee and
        90 handed back · a DRAFT sale. Owed = 500 + 0 + 0 + 0 − 50 = 450."""
        Customer.all_objects.filter(pk=self.customer.pk).update(balance=D('500'), created_at=self._ago(40))
        cash_sale = self._sale(self.cash)
        a = self._sale(self.agel)
        b = self._sale(self.agel)
        r = self.client.post('/api/finance/payments/', {'invoice': str(b.id), 'method': str(self.cash.id),
                                                        'amount': '30'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        Payment.objects.get(pk=r.data['id']).delete()           # soft-deleted → must not show
        self._draft()                                            # a DRAFT sale → must not show
        r = self.mgr.post('/api/finance/customer-receipts/', {'customer': str(self.customer.id),
                                                               'method': str(self.cash.id), 'amount': '250',
                                                               'note': 'paid at the shop'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['summary']['to_opening_balance'], '50.00')
        receipt_id = r.data['id']
        r = self.mgr.post('/api/finance/refunds/', {
            'branch': str(self.branch.id), 'original_invoice': str(cash_sale.id),
            'customer': str(self.customer.id), 'refund_method': 'CASH',
            'items': [{'variant': str(self.variant.id), 'quantity': '1', 'refund_amount': '100'}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        refund = RefundInvoice.objects.get(pk=r.data['id'])
        self.assertEqual((refund.restocking_fee, refund.paid_back), (D('10'), D('90')))
        # Spread it over time AFTER making it all (the return window would refuse a 30-day-old sale).
        self._move(cash_sale, 30)
        self._move(a, 20)
        self._move(b, 15)
        CustomerReceipt.all_objects.filter(pk=receipt_id).update(date=self._ago(10))
        RefundInvoice.all_objects.filter(pk=refund.pk).update(date=self._ago(5), created_at=self._ago(5))
        self.customer.refresh_from_db()
        return {'cash_sale': cash_sale, 'a': a, 'b': b, 'receipt_id': receipt_id}

    def test_closing_equals_customer_outstanding(self):
        self._history()
        r = self.mgr.get(URL.format(self.customer.id))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(customer_outstanding(self.customer), D('450'))
        self.assertEqual(r.data['closing_balance'], '450.00')
        self.assertEqual(r.data['current_balance'], '450.00')
        self.assertEqual(r.data['opening_balance'], '0.00')
        kinds = [ln['kind'] for ln in r.data['lines']]
        self.assertEqual(kinds, ['opening', 'sale', 'payment', 'sale', 'sale', 'receipt',
                                 'return', 'return_fee', 'paid_back'])
        self.assertEqual([ln['running_balance'] for ln in r.data['lines']],
                         ['500.00', '600.00', '500.00', '600.00', '700.00', '450.00', '350.00', '360.00', '450.00'])
        self.assertEqual(r.data['totals'], {'debit': '900.00', 'credit': '450.00'})

    def test_receipt_is_one_line_and_its_payments_are_not_counted_twice(self):
        h = self._history()
        lines = self.mgr.get(URL.format(self.customer.id)).data['lines']
        rec = [ln for ln in lines if ln['kind'] == 'receipt']
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]['credit'], rec[0]['receipt_amount'], rec[0]['method'], rec[0]['note']),
                         ('250.00', '250.00', 'Cash', 'paid at the shop'))
        self.assertEqual(Payment.objects.filter(receipt_id=h['receipt_id']).count(), 2)   # they exist…
        pays = [ln for ln in lines if ln['kind'] == 'payment']
        self.assertEqual(len(pays), 1)                                                     # …but only the till one shows
        self.assertEqual((pays[0]['credit'], pays[0]['link_id']), ('100.00', str(h['cash_sale'].id)))

    def test_date_window(self):
        self._history()
        r = self.mgr.get(URL.format(self.customer.id), {'date_from': self._day(12), 'date_to': self._day(3)})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['opening_balance'], '700.00')     # 500 + 100 − 100 + 100 + 100
        self.assertEqual([ln['kind'] for ln in r.data['lines']], ['receipt', 'return', 'return_fee', 'paid_back'])
        self.assertEqual([ln['running_balance'] for ln in r.data['lines']], ['450.00', '350.00', '360.00', '450.00'])
        self.assertEqual(r.data['closing_balance'], '450.00')
        self.assertEqual(r.data['totals'], {'debit': '100.00', 'credit': '350.00'})

        r = self.mgr.get(URL.format(self.customer.id), {'date_from': self._day(25), 'date_to': self._day(12)})
        self.assertEqual(r.data['opening_balance'], '500.00')     # opening 500, cash sale paid in full
        self.assertEqual([ln['kind'] for ln in r.data['lines']], ['sale', 'sale'])
        self.assertEqual(r.data['closing_balance'], '700.00')
        self.assertEqual(r.data['current_balance'], '450.00')     # the live balance ignores the dates

    def test_deleted_receipt_and_negative_opening(self):
        h = self._history()
        adm = _client(User.objects.create_user(username='adm_st', password='x', role='ADMIN', store=self.store))
        self.assertEqual(adm.delete(f"/api/finance/customer-receipts/{h['receipt_id']}/").status_code, 204)
        Customer.all_objects.filter(pk=self.customer.pk).update(balance=D('-20'))   # we owed THEM 20
        self.customer.refresh_from_db()
        r = self.mgr.get(URL.format(self.customer.id))
        self.assertEqual(r.data['lines'][0]['kind'], 'opening')
        self.assertEqual((r.data['lines'][0]['debit'], r.data['lines'][0]['credit']), ('0.00', '20.00'))
        self.assertNotIn('receipt', [ln['kind'] for ln in r.data['lines']])
        self.assertEqual(customer_outstanding(self.customer), D('180'))   # −20 + 0 + 100 + 100
        self.assertEqual((r.data['closing_balance'], r.data['current_balance']), ('180.00', '180.00'))

    def test_bad_dates(self):
        cid = self.customer.id
        self.assertEqual(self.mgr.get(URL.format(cid), {'date_from': '2026-02-31'}).status_code, 400)
        self.assertEqual(self.mgr.get(URL.format(cid), {'date_from': 'soon'}).status_code, 400)
        self.assertEqual(self.mgr.get(URL.format(cid), {'date_from': '2026-05-02', 'date_to': '2026-05-01'})
                         .status_code, 400)

    def test_another_shops_customer_is_404(self):
        owner2 = User.objects.create_user(username='own_st2', password='x', role='OWNER')
        store2 = Store.objects.create(name='T2', store_code='102', owner=owner2)
        theirs = Customer.objects.create(store=store2, name='Theirs', phone_number='0202', balance=D('100'))
        PaymentMethod.objects.create(store=store2, name='Cash', is_cash=True)
        self.assertEqual(self.mgr.get(URL.format(theirs.id)).status_code, 404)

    def test_cashier_is_refused(self):
        self.assertEqual(self.client.get(URL.format(self.customer.id)).status_code, 403)
