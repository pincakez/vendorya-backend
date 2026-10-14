from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from finance.models import RefundInvoice, SalesInvoice, customer_outstanding
from finance.tests import _TillBase
from inventory.models import StockLevel
from users.models import Customer, User

URL = '/api/auth/customers/'


class CustomerListBalanceTests(_TillBase):
    """§A36 (s165): the Customers list computes every balance in ONE query (an annotation) instead of
    calling customer_outstanding() once per row — and the numbers must stay exactly the same,
    returns, restocking fees and money paid back included."""

    def setUp(self):
        super().setUp()
        StockLevel.objects.filter(variant=self.variant).update(quantity=Decimal('1000'))
        self.settings.restocking_fee_percent = Decimal('10')
        self.settings.save()
        self.manager = User.objects.create_user(username='mgr_a36', password='x', role='MANAGER', store=self.store)
        self.mgr = APIClient()
        self.mgr.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.manager).access_token))
        self.n = 0

    def _sale(self, customer, method, qty=1):
        inv = self._draft(customer)
        if qty != 1:   # qty × (100 / qty) — same 100 total, room for several returns
            inv.items.update(quantity=Decimal(qty), unit_price=Decimal(100) / qty)
        self.assertEqual(self._checkout(inv, method).status_code, 200)
        inv.refresh_from_db()
        return inv

    def _pay(self, inv, amount):
        r = self.client.post('/api/finance/payments/', {'invoice': str(inv.id), 'method': str(self.cash.id),
                                                        'amount': amount}, format='json')
        self.assertEqual(r.status_code, 201, r.content)

    def _refund(self, inv, amount, method='CASH'):
        r = self.mgr.post('/api/finance/refunds/', {
            'branch': str(self.branch.id), 'original_invoice': str(inv.id), 'customer': str(inv.customer_id),
            'refund_method': method,
            'items': [{'variant': str(self.variant.id), 'quantity': '1', 'refund_amount': amount}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return RefundInvoice.objects.get(pk=r.data['id'])

    def _customer_with_history(self):
        """One customer with a bit of everything; each one a little different."""
        self.n += 1
        c = Customer.objects.create(store=self.store, name=f'Cust {self.n:02d}', phone_number=f'01{self.n:08d}',
                                    balance=Decimal(self.n * 5))                     # opening seed
        ajel = self._sale(c, self.agel)                                               # owes 100
        self._pay(ajel, str(10 * self.n))                                             # pays part of it
        self._refund(ajel, '40')                                                      # return on an unpaid part
        paid = self._sale(c, self.cash)
        fee_ref = self._refund(paid, '100')                                           # paid back 90, fee 10
        self.assertGreater(fee_ref.paid_back, 0)
        self.assertGreater(fee_ref.restocking_fee, 0)
        two = self._sale(c, self.agel, qty=2)                                         # 2 returns on ONE sale
        self._refund(two, '20')
        gone = self._refund(two, '30')
        RefundInvoice.all_objects.filter(pk=gone.pk).update(is_deleted=True)          # deleted return → ignored
        self._draft(c)                                                                # a draft → ignored
        dead = self._sale(c, self.agel)
        SalesInvoice.all_objects.filter(pk=dead.pk).update(is_deleted=True)           # deleted sale → ignored
        return c

    def _list(self):
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.data['results'] if isinstance(r.data, dict) else r.data
        return {row['id']: row['balance'] for row in rows}

    def test_list_balances_equal_customer_outstanding(self):
        customers = [self._customer_with_history() for _ in range(4)]
        balances = self._list()
        for c in customers + [self.customer, self.walkin]:
            c.refresh_from_db()
            expected = customer_outstanding(c)
            self.assertEqual(Decimal(balances[str(c.id)]), expected, c.name)
        self.assertNotEqual(len({balances[str(c.id)] for c in customers}), 1)   # really different numbers

    def test_retrieve_uses_the_same_number(self):
        c = self._customer_with_history()
        r = self.client.get(f'{URL}{c.id}/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['balance'], str(customer_outstanding(c)))

    def test_query_count_does_not_grow_with_customers(self):
        base = Customer.objects.filter(store=self.store).count()
        for _ in range(2):
            self._customer_with_history()
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(len(self._list()), base + 2)
        for _ in range(4):
            self._customer_with_history()
        with CaptureQueriesContext(connection) as big:
            self.assertEqual(len(self._list()), base + 6)
        self.assertEqual(len(big.captured_queries), len(small.captured_queries))
