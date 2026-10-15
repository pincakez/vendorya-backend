"""s171 cheques: a waiting cheque moves nothing; clearing = a normal customer / supplier payment through the cheque's
method (box line when the Treasury is on); a bounce after clearing gives the debt back; roles; other shop."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, StoreSettings
from finance.models import Cheque, CustomerReceipt, PaymentMethod, PurchaseInvoice, SupplierPayment, customer_outstanding
from finance.tests import _TillBase
from inventory.models import Supplier
from treasury import ledger
from treasury.models import MoneyBox, MoneyMove
from users.models import User

URL = '/api/finance/cheques/'
D = Decimal


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class ChequeTests(_TillBase):
    def setUp(self):
        super().setUp()
        self.manager = User.objects.create_user(username='mgr_ch', password='x', role='MANAGER', store=self.store)
        self.admin = User.objects.create_user(username='adm_ch', password='x', role='ADMIN', store=self.store)
        self.mgr = _client(self.manager)
        StoreSettings.objects.filter(store=self.store).update(cheques_enabled=True)   # s174: cheques have a switch
        self.cheque_m = PaymentMethod.objects.create(store=self.store, name='Cheque')
        self.supplier = Supplier.objects.create(store=self.store, name='Dist', code_prefix='402', prefix_locked=True)
        # the customer owes 500 (opening balance), the shop owes the supplier 800
        self.customer.balance = D('500')
        self.customer.save(update_fields=['balance'])
        PurchaseInvoice.objects.create(store=self.store, branch=self.branch, supplier=self.supplier,
                                       date=timezone.now(), status=PurchaseInvoice.Status.RECEIVED,
                                       total_amount=D('800'))

    def _new(self, direction='RECEIVED', amount='300', days=10, **extra):
        body = {'direction': direction, 'number': 'CH-1', 'bank_name': 'Bank A', 'amount': amount,
                'due_date': (timezone.localdate() + timedelta(days=days)).isoformat(),
                ('customer' if direction == 'RECEIVED' else 'supplier'):
                    str(self.customer.id if direction == 'RECEIVED' else self.supplier.id), **extra}
        r = self.mgr.post(URL, body, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def _supplier_balance(self):
        rows = self.mgr.get('/api/inventory/suppliers/').json()
        rows = rows['results'] if isinstance(rows, dict) else rows
        return D(str(next(s for s in rows if s['id'] == str(self.supplier.id))['balance']))

    def test_switch_off_blocks_new_cheques_but_keeps_written_ones(self):
        c = self._new()
        StoreSettings.objects.filter(store=self.store).update(cheques_enabled=False)
        r = self.mgr.post(URL, {'direction': 'RECEIVED', 'number': 'CH-2', 'amount': '100', 'customer': str(self.customer.id),
                                'due_date': (timezone.localdate() + timedelta(days=5)).isoformat()}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json()['code'], 'cheques_off')
        self.assertEqual(self.mgr.get(f"{URL}{c['id']}/").status_code, 200)        # a written cheque stays reachable
        r = self.mgr.post(f"{URL}{c['id']}/cancel/", {'reason': 'switched off'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)                              # …and can still be closed

    def test_waiting_cheque_moves_nothing(self):
        self._new()
        self._new('GIVEN')
        self.assertEqual(customer_outstanding(self.customer), D('500'))
        self.assertEqual(self._supplier_balance(), D('800'))
        self.assertEqual((CustomerReceipt.objects.count(), SupplierPayment.objects.count()), (0, 0))

    def test_cleared_received_cheque_pays_the_customer_debt_and_bounce_gives_it_back(self):
        c = self._new()
        r = self.mgr.post(f"{URL}{c['id']}/clear/", {'method': str(self.cheque_m.id)}, format='json')
        self.assertEqual((r.status_code, r.data['status']), (200, 'CLEARED'), r.content)
        self.assertEqual(customer_outstanding(self.customer), D('200'))
        # a manager may not bounce a cleared cheque (it reverses a payment)
        self.assertEqual(self.mgr.post(f"{URL}{c['id']}/bounce/", {'reason': 'x'}, format='json').status_code, 403)
        r = _client(self.admin).post(f"{URL}{c['id']}/bounce/", {'reason': 'no funds'}, format='json')
        self.assertEqual((r.status_code, r.data['status']), (200, 'BOUNCED'), r.content)
        self.assertEqual(customer_outstanding(self.customer), D('500'))

    def test_cleared_given_cheque_pays_the_supplier_and_bounce_gives_it_back(self):
        c = self._new('GIVEN', amount='300', method=str(self.cheque_m.id))
        r = self.mgr.post(f"{URL}{c['id']}/clear/", {}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self._supplier_balance(), D('500'))
        _client(self.admin).post(f"{URL}{c['id']}/bounce/", {'reason': 'stopped'}, format='json')
        self.assertEqual(self._supplier_balance(), D('800'))

    def test_rules(self):
        r = self.mgr.post(URL, {'direction': 'RECEIVED', 'customer': str(self.customer.id), 'number': 'X',
                                'amount': '10', 'due_date': timezone.localdate().isoformat(),
                                'method': str(self.cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'method_not_cheque'))
        c = self._new()
        r = self.mgr.post(f"{URL}{c['id']}/clear/", {}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'method_required'))
        self.assertEqual(self.mgr.post(f"{URL}{c['id']}/cancel/", {'reason': ''}, format='json').data['code'],
                         'reason_required')
        r = self.mgr.post(f"{URL}{c['id']}/cancel/", {'reason': 'typed twice'}, format='json')
        self.assertEqual(r.data['status'], 'CANCELLED')
        r = self.mgr.post(f"{URL}{c['id']}/clear/", {'method': str(self.cheque_m.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_pending'))
        self.assertEqual(self.client.get(URL).status_code, 403)          # the cashier

    def test_due_filters_and_summary(self):
        self._new(days=-2)
        self._new(days=3)
        self._new(days=30)
        self.assertEqual(len(self.mgr.get(URL, {'due': 'overdue'}).json()), 1)
        self.assertEqual(len(self.mgr.get(URL, {'due': 'soon'}).json()), 1)
        s = self.mgr.get(URL + 'summary/').json()
        self.assertEqual((s['overdue_received']['count'], s['soon_received']['count'], s['soon_given']['count']),
                         (1, 1, 0))

    def test_other_shop_cannot_see_or_touch(self):
        c = self._new()
        o = User.objects.create_user(username='own_ch2', password='x', role='OWNER')
        other = Store.objects.create(name='O', store_code='108', owner=o)
        o.store = other
        o.save(update_fields=['store'])
        oc = _client(o)
        self.assertEqual(oc.get(f"{URL}{c['id']}/").status_code, 404)
        self.assertEqual(oc.post(f"{URL}{c['id']}/cancel/", {'reason': 'x'}, format='json').status_code, 404)


class ChequeTreasuryTests(ChequeTests):
    def setUp(self):
        super().setUp()
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=True)
        ledger.setup(self.store, user=self.owner)
        self.bank = ledger.open_box(part=ledger.shop_part(self.store), name='Bank', kind=MoneyBox.Kind.BANK,
                                    opening='5000')
        self.cheque_m.lands_in = self.bank
        self.cheque_m.save(update_fields=['lands_in'])

    def test_cleared_cheques_move_the_bank_box(self):
        c = self._new()
        self.mgr.post(f"{URL}{c['id']}/clear/", {'method': str(self.cheque_m.id)}, format='json')
        self.assertEqual(ledger.balance(self.bank), D('5300'))
        g = self._new('GIVEN', amount='1000', method=str(self.cheque_m.id))
        r = self.mgr.post(f"{URL}{g['id']}/clear/", {}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(ledger.balance(self.bank), D('4300'))
        _client(self.admin).post(f"{URL}{g['id']}/bounce/", {'reason': 'stopped'}, format='json')
        self.assertEqual(ledger.balance(self.bank), D('5300'))
