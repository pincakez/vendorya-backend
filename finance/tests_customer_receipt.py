from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import ActivityLog, Store
from finance.models import CustomerReceipt, Payment, PaymentMethod, SalesInvoice, WorkShift, customer_outstanding
from finance.tests import _TillBase
from inventory.models import StockLevel
from users.models import Customer, User

URL = '/api/finance/customer-receipts/'


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class CustomerReceiptTests(_TillBase):
    """s168, PROLINE-PARITY Upgrade 2 (Proline "دفع عملاء"): a customer pays their debt as ONE lump sum,
    spread oldest-first over their unpaid sales, then the opening balance. Before: only a per-sale payment
    existed (no screen used it) and an opening-balance debt could never be paid at all."""

    def setUp(self):
        super().setUp()
        StockLevel.objects.filter(variant=self.variant).update(quantity=Decimal('1000'))
        self.manager = User.objects.create_user(username='mgr_r', password='x', role='MANAGER', store=self.store)
        self.admin = User.objects.create_user(username='adm_r', password='x', role='ADMIN', store=self.store)
        self.mgr, self.adm = _client(self.manager), _client(self.admin)

    def _ajel(self, days_ago=0, customer=None):
        inv = self._draft(customer)
        self.assertEqual(self._checkout(inv, self.agel).status_code, 200)
        SalesInvoice.all_objects.filter(pk=inv.pk).update(date=timezone.now() - timedelta(days=days_ago))
        inv.refresh_from_db()
        return inv

    def _receive(self, amount, client=None, customer=None, **extra):
        return (client or self.mgr).post(URL, {'customer': str((customer or self.customer).id),
                                               'method': str(self.cash.id), 'amount': amount, **extra},
                                         format='json')

    def _owed(self, customer=None):
        c = customer or self.customer
        c.refresh_from_db()
        return customer_outstanding(c)

    def test_lump_sum_pays_the_oldest_sale_first(self):
        newer, older = self._ajel(days_ago=1), self._ajel(days_ago=10)
        r = self._receive('150')
        self.assertEqual(r.status_code, 201, r.content)
        older.refresh_from_db(); newer.refresh_from_db()
        self.assertEqual((older.paid_amount, newer.paid_amount), (Decimal('100'), Decimal('50')))
        s = r.data['summary']
        self.assertEqual([a['invoice'] for a in s['allocations']], [str(older.id), str(newer.id)])
        self.assertEqual((s['to_invoices'], s['to_opening_balance'], s['advance']), ('150.00', '0.00', '0.00'))
        self.assertEqual((s['balance_before'], s['balance_after']), ('200.00', '50.00'))
        self.assertEqual(self._owed(), Decimal('50'))
        rec = CustomerReceipt.objects.get(pk=r.data['id'])
        self.assertEqual(Payment.objects.filter(receipt=rec).count(), 2)
        self.assertEqual(rec.unallocated, Decimal('0'))
        self.assertTrue(ActivityLog.objects.filter(store=self.store, details__customer_receipt_id=str(rec.id)).exists())

    def test_opening_balance_debt_can_be_paid(self):
        Customer.objects.filter(pk=self.customer.pk).update(balance=Decimal('500'))
        r = self._receive('200')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['summary']['to_opening_balance'], '200.00')
        self.assertEqual(r.data['summary']['allocations'], [])
        self.assertEqual(self._owed(), Decimal('300'))

    def test_sales_first_then_the_opening_balance(self):
        Customer.objects.filter(pk=self.customer.pk).update(balance=Decimal('500'))
        inv = self._ajel()
        r = self._receive('300')
        self.assertEqual(r.status_code, 201, r.content)
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('100'))
        self.assertEqual((r.data['summary']['to_invoices'], r.data['summary']['to_opening_balance']),
                         ('100.00', '200.00'))
        self.assertEqual(self._owed(), Decimal('300'))

    def test_paying_more_than_owed_is_refused_unless_kept_as_credit(self):
        self._ajel()
        r = self._receive('150')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('amount', r.data)
        self.assertFalse(CustomerReceipt.all_objects.exists())
        self.assertFalse(Payment.objects.filter(receipt__isnull=False).exists())
        self.assertEqual(self._owed(), Decimal('100'))

        r = self._receive('150', allow_advance=True)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['summary']['advance'], '50.00')
        self.assertEqual(self._owed(), Decimal('-50'))     # we owe them 50 now

    def test_deleting_a_receipt_puts_the_debt_back(self):
        Customer.objects.filter(pk=self.customer.pk).update(balance=Decimal('500'))
        a, b = self._ajel(days_ago=5), self._ajel(days_ago=1)
        r = self._receive('400')                            # 100 + 100 to the sales, 200 to the opening balance
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self._owed(), Decimal('300'))
        rid = r.data['id']
        self.assertEqual(self.mgr.delete(f'{URL}{rid}/').status_code, 403)    # ADMIN only
        self.assertEqual(self.adm.delete(f'{URL}{rid}/').status_code, 204)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.paid_amount, b.paid_amount), (Decimal('0'), Decimal('0')))
        self.assertFalse(Payment.objects.filter(receipt_id=rid).exists())        # soft-deleted, not gone
        self.assertEqual(Payment.all_objects.filter(receipt_id=rid, is_deleted=True).count(), 2)
        self.assertTrue(CustomerReceipt.all_objects.get(pk=rid).is_deleted)
        self.assertEqual(self._owed(), Decimal('700'))
        data = self.mgr.get(URL).data
        self.assertEqual(list(data['results'] if isinstance(data, dict) else data), [])

    def test_another_shops_customer_or_method_is_refused(self):
        owner2 = User.objects.create_user(username='own_r2', password='x', role='OWNER')
        store2 = Store.objects.create(name='T2', store_code='102', owner=owner2)
        theirs = Customer.objects.create(store=store2, name='Theirs', phone_number='0202', balance=Decimal('100'))
        their_cash = PaymentMethod.objects.create(store=store2, name='Cash', is_cash=True)
        self.assertEqual(self._receive('50', customer=theirs).status_code, 400)
        r = self.mgr.post(URL, {'customer': str(self.customer.id), 'method': str(their_cash.id), 'amount': '1'},
                          format='json')
        self.assertEqual(r.status_code, 400)
        theirs.refresh_from_db()
        self.assertEqual(customer_outstanding(theirs), Decimal('100'))
        rec = CustomerReceipt.objects.create(store=store2, customer=theirs, amount=Decimal('1'), method=their_cash)
        self.assertEqual(self.mgr.get(f'{URL}{rec.id}/').status_code, 404)
        self.assertEqual(self.adm.delete(f'{URL}{rec.id}/').status_code, 404)

    def test_cashier_cannot_receive_or_list(self):
        self._ajel()
        self.assertEqual(self._receive('50', client=self.client).status_code, 403)
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(self._owed(), Decimal('100'))

    def test_ajel_is_not_a_way_to_pay(self):
        self._ajel()
        r = self.mgr.post(URL, {'customer': str(self.customer.id), 'method': str(self.agel.id), 'amount': '50'},
                          format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._receive('0').status_code, 400)

    def test_cash_received_counts_in_the_managers_drawer(self):
        Customer.objects.filter(pk=self.customer.pk).update(balance=Decimal('500'))
        self._ajel()
        shift = WorkShift.objects.create(store=self.store, branch=self.branch, user=self.manager)
        self.assertEqual(self._receive('300').status_code, 201)
        self.assertEqual(shift.cash_in(), Decimal('300'))    # 100 on the sale + 200 on the opening balance
        r = self.mgr.get('/api/finance/shifts/drawer/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['cash_in'], '300.00')
        self.assertEqual(sum(Decimal(m['amount']) for m in r.data['moves'] if m['kind'] == 'IN'), Decimal('300'))

    def test_list_filters_by_customer(self):
        other = Customer.objects.create(store=self.store, name='Other', phone_number='0303', balance=Decimal('50'))
        Customer.objects.filter(pk=self.customer.pk).update(balance=Decimal('50'))
        self._receive('10'); self._receive('10', customer=other)
        r = self.mgr.get(URL, {'customer': str(other.id)})
        rows = r.data['results'] if isinstance(r.data, dict) else r.data
        self.assertEqual([row['customer'] for row in rows], [other.id])


class CustomerOpeningBalanceTests(_TillBase):
    """s168, PROLINE-PARITY Upgrade 4: the opening balance (what a customer owed before Vendorya) is set on the
    customer form — by a manager, never a cashier (a cashier could otherwise wipe a debt)."""
    CURL = '/api/auth/customers/'

    def setUp(self):
        super().setUp()
        self.manager = User.objects.create_user(username='mgr_o', password='x', role='MANAGER', store=self.store)
        self.mgr = _client(self.manager)

    def test_manager_sets_and_edits_the_opening_balance(self):
        r = self.mgr.post(self.CURL, {'name': 'Old debtor', 'opening_balance': '750'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.data['opening_balance'], r.data['balance']), ('750.00', '750.00'))
        cid = r.data['id']
        r = self.mgr.patch(f'{self.CURL}{cid}/', {'opening_balance': '-20'}, format='json')   # we owed THEM
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Customer.objects.get(pk=cid).balance, Decimal('-20'))
        self.assertEqual(ActivityLog.objects.filter(store=self.store, details__customer_id=cid).count(), 2)

    def test_cashier_cannot_set_or_change_it(self):
        r = self.client.post(self.CURL, {'name': 'Sneaky', 'opening_balance': '500'}, format='json')
        self.assertEqual(r.status_code, 403, r.content)
        Customer.objects.filter(pk=self.customer.pk).update(balance=Decimal('300'))
        r = self.client.patch(f'{self.CURL}{self.customer.id}/', {'opening_balance': '0'}, format='json')
        self.assertEqual(r.status_code, 403, r.content)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, Decimal('300'))
        # …but the rest of the form still works for them, even when the unchanged value comes along
        r = self.client.patch(f'{self.CURL}{self.customer.id}/', {'notes': 'vip', 'opening_balance': '300'},
                              format='json')
        self.assertEqual(r.status_code, 200, r.content)
        r = self.client.post(self.CURL, {'name': 'New at the till'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
