"""Return goods to a supplier (s171, finance/purchase_returns.py): stock out, caps, serial units, supplier account +
statement + A/P aging, money back into a box, void undoes everything, average cost, roles, other shop."""
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, Branch, Address, StoreSettings
from finance.models import PaymentMethod, PurchaseReturn, weighted_avg_cost
from inventory.models import Product, ProductVariant, Supplier, StockLevel, SerialUnit
from treasury import ledger
from treasury.models import MoneyBox, MoneyMove
from users.models import User

URL = '/api/finance/purchase-returns/'
D = Decimal


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')
    return c


class PurchaseReturnTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_pr', password='x', role='OWNER')
        self.store = Store.objects.create(name='Returns shop', store_code='141', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        StoreSettings.objects.get_or_create(store=self.store)
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        self.manager = User.objects.create_user(username='mgr_pr', password='x', role='MANAGER', store=self.store)
        self.admin = User.objects.create_user(username='adm_pr', password='x', role='ADMIN', store=self.store)
        self.cashier = User.objects.create_user(username='csh_pr', password='x', role='CASHIER', store=self.store)
        self.supplier = Supplier.objects.create(store=self.store, name='Dist', code_prefix='441', prefix_locked=True)
        self.other_supplier = Supplier.objects.create(store=self.store, name='Other', code_prefix='442',
                                                      prefix_locked=True)
        self.product = Product.objects.create(store=self.store, name='Mouse', supplier=self.supplier)
        self.variant = ProductVariant.objects.create(product=self.product, sell_price=D('300'))
        self.laptop = Product.objects.create(store=self.store, name='Laptop', supplier=self.supplier,
                                             track_serials=True)
        self.lvar = ProductVariant.objects.create(product=self.laptop, sell_price=D('20000'))
        self.cash = PaymentMethod.all_objects.filter(store=self.store, is_cash=True).first() or \
            PaymentMethod.objects.create(store=self.store, name='Cash', is_cash=True)
        self.mgr = _client(self.manager)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _buy(self, lines, supplier=None):
        r = self.mgr.post('/api/finance/purchases/', {
            'supplier': str((supplier or self.supplier).id), 'branch': str(self.branch.id),
            'date': timezone.now().isoformat(), 'lines': lines}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        r2 = self.mgr.post(f"/api/finance/purchases/{r.data['id']}/receive/", {}, format='json')
        self.assertEqual(r2.status_code, 200, r2.content)
        return r.data['id']

    def _returnable(self, pid):
        r = self.mgr.get(URL + f'returnable/?purchase={pid}')
        self.assertEqual(r.status_code, 200, r.content)
        return r.data['lines']

    def _stock(self, variant=None):
        s = StockLevel.objects.filter(variant=variant or self.variant, branch=self.branch).first()
        return D(str(s.quantity)) if s else D('0')

    def _balance(self):
        rows = self.mgr.get('/api/inventory/suppliers/').json()
        rows = rows['results'] if isinstance(rows, dict) else rows
        return D(str(next(s for s in rows if s['id'] == str(self.supplier.id))['balance']))

    def _return(self, pid, qty, client=None, **extra):
        line = self._returnable(pid)[0]
        body = {'purchase': pid, 'lines': [{'purchase_item': line['purchase_item'], 'quantity': str(qty),
                                            **extra.pop('line', {})}], **extra}
        return (client or self.mgr).post(URL, body, format='json')

    # ── the basics ──────────────────────────────────────────────────────────
    def test_return_takes_stock_out_and_lowers_the_supplier_account(self):
        pid = self._buy([{'variant': str(self.variant.id), 'quantity': '10', 'base_price': '100'}])
        self.assertEqual((self._stock(), self._balance()), (D('10'), D('1000.00')))
        r = self._return(pid, 4, notes='broken box')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.data['return_number'], r.data['total_amount'], r.data['credit_amount']),
                         (1, '400.00', '400.00'))
        self.assertEqual((self._stock(), self._balance()), (D('6'), D('600.00')))
        line = self._returnable(pid)[0]
        self.assertEqual((D(line['returned']), D(line['left'])), (D('4'), D('6')))

    def test_cannot_return_more_than_bought_or_more_than_on_hand(self):
        pid = self._buy([{'variant': str(self.variant.id), 'quantity': '5', 'base_price': '100'}])
        r = self._return(pid, 6)
        self.assertEqual((r.status_code, r.data['code']), (400, 'more_than_bought'))
        self.assertEqual(self._return(pid, 3).status_code, 201)
        r = self._return(pid, 3)
        self.assertEqual((r.status_code, r.data['code']), (400, 'more_than_bought'))
        # 2 left on the purchase, but sell them first → nothing on hand
        StockLevel.objects.filter(variant=self.variant, branch=self.branch).update(quantity=1)
        r = self._return(pid, 2)
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_stock'))
        self.assertEqual(PurchaseReturn.objects.count(), 1)          # the refused ones left nothing behind
        self.assertEqual(self._stock(), D('1'))

    def test_statement_and_ap_aging_follow_the_return(self):
        pid = self._buy([{'variant': str(self.variant.id), 'quantity': '10', 'base_price': '100'}])
        self._return(pid, 2, refund_amount='50', refund_method=str(self.cash.id))
        st = self.mgr.get(f'/api/inventory/suppliers/{self.supplier.id}/statement/').json()
        kinds = [(l['kind'], l['debit'], l['credit']) for l in st['lines']]
        self.assertIn(('return', '200.00', '0.00'), kinds)
        self.assertIn(('return_refund', '0.00', '50.00'), kinds)
        self.assertEqual(st['closing_balance'], '850.00')
        self.assertEqual(self._balance(), D('850.00'))
        ap = self.mgr.get('/api/reports/ap-aging/').json()
        row = next(r for r in ap['rows'] if r['supplier_id'] == str(self.supplier.id))
        self.assertEqual(row['total'], '850.00')

    def test_ap_aging_now_counts_supplier_payments(self):
        self._buy([{'variant': str(self.variant.id), 'quantity': '10', 'base_price': '100'}])
        r = self.mgr.post('/api/finance/supplier-payments/', {
            'supplier': str(self.supplier.id), 'amount': '300', 'payment_method': str(self.cash.id)}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        ap = self.mgr.get('/api/reports/ap-aging/').json()
        row = next(r for r in ap['rows'] if r['supplier_id'] == str(self.supplier.id))
        self.assertEqual(row['total'], '700.00')

    def test_wrong_supplier_and_other_shop_are_refused(self):
        pid = self._buy([{'variant': str(self.variant.id), 'quantity': '2', 'base_price': '100'}])
        line = self._returnable(pid)[0]
        r = self.mgr.post(URL, {'purchase': pid, 'supplier': str(self.other_supplier.id),
                                'lines': [{'purchase_item': line['purchase_item'], 'quantity': '1'}]}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'wrong_supplier'))
        o_owner = User.objects.create_user(username='own_pr2', password='x', role='OWNER')
        other = Store.objects.create(name='Other shop', store_code='142', owner=o_owner)
        o_owner.store = other
        o_owner.save(update_fields=['store'])
        oc = _client(o_owner)
        self.assertEqual(oc.get(URL + f'returnable/?purchase={pid}').status_code, 404)
        r = oc.post(URL, {'purchase': pid, 'lines': [{'purchase_item': line['purchase_item'], 'quantity': '1'}]},
                    format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._stock(), D('2'))

    def test_roles(self):
        pid = self._buy([{'variant': str(self.variant.id), 'quantity': '2', 'base_price': '100'}])
        self.assertEqual(self._return(pid, 1, client=_client(self.cashier)).status_code, 403)
        rid = self._return(pid, 1).data['id']
        self.assertEqual(self.mgr.post(URL + f'{rid}/void/', {'reason': 'x'}, format='json').status_code, 403)
        self.assertIn(self.mgr.delete(URL + f'{rid}/').status_code, (403, 405))   # no delete — void only
        self.assertEqual(self.mgr.get(URL + 'not-a-uuid/').status_code, 404)

    # ── void ────────────────────────────────────────────────────────────────
    def test_void_undoes_stock_and_account_and_needs_a_reason(self):
        pid = self._buy([{'variant': str(self.variant.id), 'quantity': '10', 'base_price': '100'}])
        rid = self._return(pid, 4).data['id']
        adm = _client(self.admin)
        r = adm.post(URL + f'{rid}/void/', {'reason': ' '}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'reason_required'))
        r = adm.post(URL + f'{rid}/void/', {'reason': 'typed the wrong qty'}, format='json')
        self.assertEqual((r.status_code, r.data['status']), (200, 'VOID'))
        self.assertEqual((self._stock(), self._balance()), (D('10'), D('1000.00')))
        self.assertEqual(D(self._returnable(pid)[0]['left']), D('10'))
        r = adm.post(URL + f'{rid}/void/', {'reason': 'again'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'already_void'))

    # ── serial units ────────────────────────────────────────────────────────
    def test_serial_units_go_back_and_come_back_on_void(self):
        pid = self._buy([{'variant': str(self.lvar.id), 'quantity': '3', 'base_price': '15000',
                          'serial_numbers': ['S-1', 'S-2', 'S-3']}])
        r = self._return(pid, 1, line={'serial_numbers': []})
        self.assertEqual((r.status_code, r.data['serial_code']), (400, 'serial_count'), r.content)
        r = self._return(pid, 1, line={'serial_numbers': ['S-9']})
        self.assertEqual(r.status_code, 400)
        r = self._return(pid, 1, line={'serial_numbers': ['s-2']})
        self.assertEqual(r.status_code, 201, r.content)
        u = SerialUnit.objects.get(serial='S-2')
        self.assertEqual(u.status, 'SUPPLIER_RETURNED')
        self.assertEqual(self._stock(self.lvar), D('2'))
        _client(self.admin).post(URL + f"{r.data['id']}/void/", {'reason': 'wrong unit'}, format='json')
        u.refresh_from_db()
        self.assertEqual((u.status, self._stock(self.lvar)), ('IN_STOCK', D('3')))

    # ── average cost ────────────────────────────────────────────────────────
    def test_returned_goods_leave_the_average_cost(self):
        self._buy([{'variant': str(self.variant.id), 'quantity': '10', 'base_price': '100'}])
        pid2 = self._buy([{'variant': str(self.variant.id), 'quantity': '10', 'base_price': '200'}])
        self.assertEqual(weighted_avg_cost(self.variant, self.store), D('150.00'))
        self._return(pid2, 10)
        self.assertEqual(weighted_avg_cost(self.variant, self.store), D('100.00'))

    # ── a free return (no purchase) ─────────────────────────────────────────
    def test_free_return_uses_the_average_cost(self):
        self._buy([{'variant': str(self.variant.id), 'quantity': '4', 'base_price': '100'}])
        r = self.mgr.post(URL, {'supplier': str(self.supplier.id), 'branch': str(self.branch.id),
                                'lines': [{'variant': str(self.variant.id), 'quantity': '1'}]}, format='json')
        self.assertEqual((r.status_code, r.data['total_amount']), (201, '100.00'), r.content)
        self.assertEqual(self._stock(), D('3'))


class PurchaseReturnTreasuryTests(PurchaseReturnTests):
    """Same shop with the Treasury on: money back is a SUPPLIER_IN line; a void reverses it."""

    def setUp(self):
        super().setUp()
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=True)
        ledger.setup(self.store, user=self.owner)
        # money in the drawer, so a supplier payment from it (inherited tests) can leave
        drawer = MoneyBox.all_objects.get(store=self.store, branch=self.branch, is_default_drawer=True)
        ledger.post(drawer, D('5000'), MoneyMove.Kind.OPENING, check_floor=False)

    def test_money_back_lands_in_a_box_and_void_reverses_it(self):
        pid = self._buy([{'variant': str(self.variant.id), 'quantity': '10', 'base_price': '100'}])
        drawer = MoneyBox.all_objects.get(store=self.store, branch=self.branch, is_default_drawer=True)
        before = ledger.balance(drawer)
        r = self._return(pid, 3, refund_amount='300', refund_method=str(self.cash.id))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((str(r.data['refund_box']), r.data['credit_amount']), (str(drawer.id), '0.00'))
        self.assertEqual(ledger.balance(drawer), before + D('300'))
        self.assertEqual(MoneyMove.all_objects.filter(purchase_return_id=r.data['id'], kind='SUPPLIER_IN').count(), 1)
        # all 300 came back as cash, none as credit → we still owe the full 1000 (cash instead of credit)
        self.assertEqual(self._balance(), D('1000.00'))
        _client(self.admin).post(URL + f"{r.data['id']}/void/", {'reason': 'oops'}, format='json')
        self.assertEqual(ledger.balance(drawer), before)
        self.assertEqual(self._balance(), D('1000.00'))

    def test_money_back_needs_a_real_method_and_not_more_than_the_return(self):
        pid = self._buy([{'variant': str(self.variant.id), 'quantity': '10', 'base_price': '100'}])
        r = self._return(pid, 1, refund_amount='50')
        self.assertEqual((r.status_code, r.data['code']), (400, 'refund_method_required'))
        r = self._return(pid, 1, refund_amount='150', refund_method=str(self.cash.id))
        self.assertEqual((r.status_code, r.data['code']), (400, 'refund_above_total'))
        self.assertEqual(self._stock(), D('10'))
