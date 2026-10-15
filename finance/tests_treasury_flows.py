"""Treasury Ledger Wave 1, part B (TREASURY.md § 4 + § 11 B): every money flow writes its box line, in the same
transaction — and with the switch OFF nothing is ever written."""
from datetime import date
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, Branch, Address, StoreSettings
from finance.models import (
    PaymentMethod, SalesInvoice, SalesInvoiceItem, WorkShift, Expense, ExpenseCategory, SupplierPayment,
    RefundInvoice, CustomerReceipt,
)
from inventory.models import Supplier, Product, ProductVariant, StockLevel
from notifications.models import Notification
from treasury import ledger
from treasury.models import MoneyBox, MoneyMove
from users.models import User, Customer

D = Decimal
K = MoneyMove.Kind


class _Base(TestCase):
    TREASURY = True
    CODE = '301'

    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        c = self.CODE
        self.owner = User.objects.create_user(username=f'own{c}', password='x', role='OWNER')
        self.store = Store.objects.create(name=f'Shop {c}', store_code=c, owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.manager = User.objects.create_user(username=f'mgr{c}', password='x', role='MANAGER', store=self.store)
        self.cashier = User.objects.create_user(username=f'csh{c}', password='x', role='CASHIER', store=self.store)
        self.branch = Branch.objects.create(store=self.store, name='Main',
                                            address=Address.objects.create(store=self.store, street_1='1', city='C'))
        self.sup = Supplier.objects.create(store=self.store, name='Sup', code_prefix=c, prefix_locked=True)
        product = Product.objects.create(store=self.store, name='Mouse', supplier=self.sup)
        self.variant = ProductVariant.objects.create(product=product, sell_price=D('100'))
        StockLevel.objects.create(variant=self.variant, branch=self.branch, quantity=D('100'))
        self.customer = Customer.objects.create(store=self.store, name='Buyer', phone_number='0101')
        Customer.objects.create(store=self.store, name='Walk-in', phone_number='0', is_walk_in=True)
        self.cash = PaymentMethod.objects.create(store=self.store, name='Cash', is_cash=True)
        self.card = PaymentMethod.objects.create(store=self.store, name='Card')
        self.agel = PaymentMethod.objects.create(store=self.store, name='Ajel', is_agel=True)
        self.cat = ExpenseCategory.objects.create(store=self.store, name='Rent')
        StoreSettings.objects.update_or_create(store=self.store, defaults={'treasury_enabled': self.TREASURY})
        if self.TREASURY:
            self.part = ledger.setup(self.store)
            self.drawer = ledger.default_drawer(self.branch)
            self.safe = ledger.open_box(part=self.part, name='Safe', kind=MoneyBox.Kind.CASH, opening='1000')
            self.bank = ledger.open_box(part=self.part, name='Bank', kind=MoneyBox.Kind.BANK)

    # ── helpers ──
    def api(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
        return client

    def bal(self, box):
        return ledger.balance(box)

    def draft(self, total='100', qty='1', customer=None):
        inv = SalesInvoice.objects.create(store=self.store, branch=self.branch, customer=customer or self.customer,
                                          status=SalesInvoice.Status.DRAFT, date=timezone.now())
        SalesInvoiceItem.objects.create(invoice=inv, variant=self.variant, quantity=D(qty), unit_price=D(total) / D(qty))
        SalesInvoice.objects.filter(pk=inv.pk).update(grand_total=D(total))
        inv.refresh_from_db()
        return inv

    def sell(self, user, lines, total='100', qty='1', customer=None):
        inv = self.draft(total, qty, customer)
        body = {'payments': [{'method': str(m.id), 'amount': a} for m, a in lines]}
        r = self.api(user).post(f'/api/finance/invoices/{inv.id}/checkout/', body, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        return inv

    def open_shift(self, user, starting='0', **extra):
        return self.api(user).post('/api/finance/shifts/', {'branch': str(self.branch.id), 'starting_cash': starting,
                                                            **extra}, format='json')

    def refund(self, user, inv, amount, qty='1', method='CASH'):
        return self.api(user).post('/api/finance/refunds/', {
            'branch': str(self.branch.id), 'original_invoice': str(inv.id), 'customer': str(inv.customer_id),
            'refund_method': method,
            'items': [{'variant': str(self.variant.id), 'quantity': qty, 'refund_amount': amount}],
        }, format='json')

    def expense(self, user, amount, paid_from=None):
        body = {'branch': str(self.branch.id), 'category': str(self.cat.id), 'amount': amount, 'date': str(date.today())}
        if paid_from is not None:
            body['paid_from'] = str(paid_from.id)
        return self.api(user).post('/api/finance/expenses/', body, format='json')

    def lines(self, **f):
        return MoneyMove.all_objects.filter(**f)


class CrossCheckTests(_Base):
    def test_box_shift_book_equals_old_formula_to_the_cent(self):
        """THE money rule: a full shift on the drawer box — cash sale (with change), split sale (cash + card), cash
        refund, cash customer receipt (part pays a sale, part advance) — closes with expected cash = the OLD
        formula (starting_cash + cash_in − cash_out), to the cent, = the drawer's balance."""
        r = self.open_shift(self.manager, '100.50', count_reason='Float from the safe')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.data['ledger'], str(r.data['drawer']), r.data['book_balance']),
                         ('BOX', str(self.drawer.id), '0.00'))
        shift = WorkShift.all_objects.get(pk=r.data['id'])

        s1 = self.sell(self.manager, [(self.cash, '150')], total='99.99')            # 50.01 change
        self.sell(self.manager, [(self.cash, '600.25'), (self.card, '399.75')], total='1000', qty='2')
        r = self.refund(self.manager, s1, '99.99')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(D(r.data['drawer_cash']), D('99.99'))
        self.sell(self.manager, [(self.agel, '1')], total='300')                     # Ajel: no line
        r = self.api(self.manager).post('/api/finance/customer-receipts/', {
            'customer': str(self.customer.id), 'amount': '400.10', 'method': str(self.cash.id),
            'allow_advance': True}, format='json')
        self.assertEqual(r.status_code, 201, r.content)

        shift.refresh_from_db()
        old = shift.starting_cash + shift.cash_in() - shift.cash_out()
        self.assertEqual(old, D('100.50') + D('99.99') + D('600.25') - D('99.99') + D('400.10'))
        self.assertEqual(self.bal(self.drawer), old)
        self.assertEqual(self.bal(ledger.unsorted_box(self.store)), D('399.75'))   # card, no "lands in" yet
        # one receipt line for the whole amount — its sale share is not posted again
        self.assertEqual(self.lines(kind=K.RECEIPT_IN).count(), 1)
        self.assertEqual(self.lines(kind=K.SALE_IN, payment__receipt__isnull=False).count(), 0)

        d = self.api(self.manager).get('/api/finance/shifts/drawer/').data
        self.assertEqual(D(d['expected_balance']), old)
        self.assertEqual(D(d['cash_in']) - D(d['cash_out']), old - shift.starting_cash)

        c = self.api(self.manager).post(f'/api/finance/shifts/{shift.id}/close/', {'counted_cash': str(old)},
                                        format='json')
        self.assertEqual(c.status_code, 200, c.content)
        self.assertEqual(D(c.data['expected_cash']), old)
        self.assertEqual(D(c.data['difference']), D('0'))
        self.assertEqual(self.bal(self.drawer), old)


class ShiftDrawerTests(_Base):
    def test_carry_over_count_and_move_takings_to_the_safe(self):
        r = self.open_shift(self.cashier, '0')
        sid = r.data['id']
        self.sell(self.cashier, [(self.cash, '100')])
        # close: count 100 (= book) and move 60 to the safe → 40 stays as the float
        c = self.api(self.cashier).post(f'/api/finance/shifts/{sid}/close/', {
            'counted_cash': '100', 'move_to_box': str(self.safe.id), 'move_amount': '60'}, format='json')
        self.assertEqual(c.status_code, 200, c.content)
        self.assertEqual((self.bal(self.drawer), self.bal(self.safe)), (D('40'), D('1060')))
        self.assertEqual(self.lines(kind=K.TRANSFER_OUT, box=self.drawer).count(), 1)

        # the next shift sees the carried-over 40 before opening
        d = self.api(self.manager).get(f'/api/finance/shifts/drawers/?branch={self.branch.id}')
        self.assertEqual(d.status_code, 200, d.content)
        self.assertEqual([(x['id'], x['book_balance'], x['busy']) for x in d.data],
                         [(str(self.drawer.id), '40.00', False)])
        # counted 35 ≠ book 40 → a reason is required, and nothing is kept without it
        r = self.open_shift(self.manager, '35')
        self.assertEqual((r.status_code, r.data['code']), (400, 'reason_required'), r.content)
        self.assertFalse(WorkShift.all_objects.filter(user=self.manager).exists())
        r = self.open_shift(self.manager, '35', count_reason='5 short')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.data['book_balance'], D(r.data['starting_cash'])), ('40.00', D('35')))
        adj = self.lines(kind=K.COUNT_ADJUST, box=self.drawer).get()
        self.assertEqual((adj.amount, adj.reason, str(adj.shift_id)), (D('-5'), '5 short', r.data['id']))
        self.assertEqual(self.bal(self.drawer), D('35'))
        # the drawer is busy now: nobody else opens on it
        # privacy: a cashier must name the branch, and sees THAT it is busy, never who holds it
        r = self.api(self.cashier).get('/api/finance/shifts/drawers/')
        self.assertEqual((r.status_code, r.data['code']), (400, 'branch_required'))
        d = self.api(self.cashier).get(f'/api/finance/shifts/drawers/?branch={self.branch.id}').data
        self.assertTrue(d[0]['busy'])
        self.assertEqual(d[0]['book_balance'], '35.00')
        self.assertNotIn('busy_by', d[0])
        d = self.api(self.owner).get('/api/finance/shifts/drawers/').data
        self.assertEqual(d[0]['busy_by'], self.manager.username)
        r = self.open_shift(self.cashier, '35')
        self.assertEqual((r.status_code, r.data['code']), (400, 'drawer_busy'), r.content)

    def test_close_difference_needs_reason_then_count_adjust_and_notification(self):
        sid = self.open_shift(self.cashier, '0').data['id']
        self.sell(self.cashier, [(self.cash, '100')])
        url = f'/api/finance/shifts/{sid}/close/'
        r = self.api(self.cashier).post(url, {'counted_cash': '90'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'reason_required'), r.content)
        self.assertEqual(WorkShift.all_objects.get(pk=sid).status, 'OPEN')
        r = self.api(self.cashier).post(url, {'counted_cash': '90', 'count_reason': 'unknown'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((D(r.data['expected_cash']), D(r.data['difference'])), (D('100'), D('-10')))
        self.assertEqual(self.lines(kind=K.COUNT_ADJUST).get().amount, D('-10'))
        self.assertEqual(self.bal(self.drawer), D('90'))
        self.assertTrue(Notification.all_objects.filter(store=self.store, type='SHIFT_DIFFERENCE').exists())

    def test_move_more_than_counted_refused_and_nothing_kept(self):
        sid = self.open_shift(self.cashier, '0').data['id']
        self.sell(self.cashier, [(self.cash, '100')])
        r = self.api(self.cashier).post(f'/api/finance/shifts/{sid}/close/', {
            'counted_cash': '100', 'move_to_box': str(self.safe.id), 'move_amount': '150'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'), r.content)
        self.assertEqual(WorkShift.all_objects.get(pk=sid).status, 'OPEN')
        self.assertEqual((self.bal(self.drawer), self.bal(self.safe)), (D('100'), D('1000')))

    def test_legacy_shift_unchanged_when_open_before_the_switch(self):
        shift = WorkShift.all_objects.create(store=self.store, branch=self.branch, user=self.cashier,
                                             starting_cash=D('10'))
        self.assertEqual(shift.ledger, 'LEGACY')
        self.sell(self.cashier, [(self.cash, '100')])
        self.assertEqual(self.bal(self.drawer), D('100'))     # the cash still lands in the branch drawer
        r = self.api(self.cashier).post(f'/api/finance/shifts/{shift.id}/close/', {'counted_cash': '110'},
                                        format='json')
        self.assertEqual((D(r.data['expected_cash']), D(r.data['difference'])), (D('110'), D('0')))
        self.assertEqual(self.lines(kind=K.COUNT_ADJUST).count(), 0)


class PaidOutTests(_Base):
    def test_cash_expense_from_the_drawer_lowers_expected_cash(self):
        sid = self.open_shift(self.manager, '0').data['id']
        self.sell(self.manager, [(self.cash, '100')])
        r = self.expense(self.manager, '30')
        self.assertEqual((r.status_code, r.data['code']), (400, 'paid_from_required'), r.content)
        r = self.expense(self.manager, '500', self.drawer)                     # more than the drawer holds
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'), r.content)
        self.assertEqual(Expense.all_objects.count(), 0)
        r = self.expense(self.manager, '30', self.drawer)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['paid_from_name'], self.drawer.name)
        d = self.api(self.manager).get('/api/finance/shifts/drawer/').data
        self.assertEqual(D(d['expected_balance']), D('70'))
        self.assertIn('EXPENSE_OUT', [m['line_kind'] for m in d['moves']])
        c = self.api(self.manager).post(f'/api/finance/shifts/{sid}/close/', {'counted_cash': '70'}, format='json')
        self.assertEqual((D(c.data['expected_cash']), D(c.data['difference'])), (D('70'), D('0')))

    def test_supplier_cash_payment_defaults_to_my_drawer_and_card_to_chosen_box(self):
        self.open_shift(self.manager, '0')
        self.sell(self.manager, [(self.cash, '100')])
        r = self.api(self.manager).post('/api/finance/supplier-payments/', {
            'supplier': str(self.sup.id), 'amount': '40', 'payment_method': str(self.cash.id)}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(str(r.data['paid_from']), str(self.drawer.id))
        self.assertEqual(self.bal(self.drawer), D('60'))
        r = self.api(self.manager).post('/api/finance/supplier-payments/', {
            'supplier': str(self.sup.id), 'amount': '250', 'payment_method': str(self.card.id),
            'paid_from': str(self.bank.id)}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.bal(self.bank), D('-250'))                         # a bank box may run an overdraft
        # delete → the money comes back
        r = self.api(self.owner).delete(f'/api/finance/supplier-payments/{r.data["id"]}/')
        self.assertEqual(r.status_code, 204, r.content)
        self.assertEqual(self.bal(self.bank), D('0'))
        self.assertEqual(self.lines(kind=K.REVERSAL, box=self.bank).count(), 1)

    def test_supplier_cash_payment_with_no_shift_comes_from_the_branch_drawer(self):
        # no open shift + no "lands in" → the card default is "not sorted yet"; cash with no shift → branch drawer
        r = self.api(self.manager).post('/api/finance/supplier-payments/', {
            'supplier': str(self.sup.id), 'amount': '10', 'payment_method': str(self.cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'), r.content)
        self.assertEqual(SupplierPayment.all_objects.count(), 0)

    def test_expense_edit_and_delete_reverse(self):
        r = self.expense(self.manager, '50', self.safe)
        eid = r.data['id']
        self.assertEqual(self.bal(self.safe), D('950'))
        api = self.api(self.manager)
        self.assertEqual(api.patch(f'/api/finance/expenses/{eid}/', {'amount': '80'}, format='json').status_code, 200)
        self.assertEqual(self.bal(self.safe), D('920'))
        self.assertEqual(api.patch(f'/api/finance/expenses/{eid}/', {'paid_from': str(self.bank.id)},
                                   format='json').status_code, 200)
        self.assertEqual((self.bal(self.safe), self.bal(self.bank)), (D('1000'), D('-80')))
        # a description edit writes nothing
        n = MoneyMove.all_objects.count()
        api.patch(f'/api/finance/expenses/{eid}/', {'description': 'June'}, format='json')
        self.assertEqual(MoneyMove.all_objects.count(), n)
        r = api.patch(f'/api/finance/expenses/{eid}/', {'paid_from': None}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'paid_from_required'))
        self.assertEqual(self.api(self.owner).delete(f'/api/finance/expenses/{eid}/').status_code, 204)
        self.assertEqual(self.bal(self.bank), D('0'))
        self.assertEqual(Expense.all_objects.get(pk=eid).is_deleted, True)
        live = MoneyMove.all_objects.filter(expense_id=eid).exclude(kind=K.REVERSAL).filter(reversed_by__isnull=True)
        self.assertEqual(live.count(), 0)

    def test_expense_made_before_the_treasury_stays_out_of_boxes(self):
        old = Expense.all_objects.create(store=self.store, branch=self.branch, category=self.cat, amount=D('20'),
                                         date=date.today())
        api = self.api(self.manager)
        self.assertEqual(api.patch(f'/api/finance/expenses/{old.id}/', {'amount': '25'}, format='json').status_code,
                         200)
        r = api.patch(f'/api/finance/expenses/{old.id}/', {'paid_from': str(self.safe.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'before_treasury'))
        self.assertEqual(self.api(self.owner).delete(f'/api/finance/expenses/{old.id}/').status_code, 204)
        self.assertEqual(MoneyMove.all_objects.filter(expense=old).count(), 0)


class LandsInTests(_Base):
    def test_card_lands_in_its_box_unsorted_without_one_and_ajel_nowhere(self):
        self.open_shift(self.cashier, '0')
        self.sell(self.cashier, [(self.card, '100')])
        unsorted = ledger.unsorted_box(self.store)
        self.assertEqual(self.bal(unsorted), D('100'))
        r = self.api(self.owner).patch(f'/api/finance/payment-methods/{self.card.id}/',
                                       {'lands_in': str(self.bank.id)}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['lands_in_name'], 'Bank')
        self.sell(self.cashier, [(self.card, '100')])
        self.assertEqual((self.bal(self.bank), self.bal(unsorted)), (D('100'), D('100')))
        n = MoneyMove.all_objects.count()
        inv = self.sell(self.cashier, [(self.agel, '1')])
        self.assertEqual(MoneyMove.all_objects.count(), n)                     # Ajel: no money moved
        # the debt paid later on that sale (POST /payments/) lands like any payment
        r = self.api(self.cashier).post('/api/finance/payments/', {'invoice': str(inv.id), 'method': str(self.cash.id),
                                                                   'amount': '30'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.bal(self.drawer), D('30'))

    def test_lands_in_rules(self):
        url = f'/api/finance/payment-methods/{self.card.id}/'
        api = self.api(self.owner)
        r = api.patch(url, {'lands_in': str(self.drawer.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'drawer_not_allowed'))
        r = api.patch(f'/api/finance/payment-methods/{self.cash.id}/', {'lands_in': str(self.bank.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'lands_in_not_allowed'))
        r = api.patch(f'/api/finance/payment-methods/{self.agel.id}/', {'lands_in': str(self.bank.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'lands_in_not_allowed'))
        self.assertEqual(api.patch(url, {'lands_in': None}, format='json').status_code, 200)


class ReceiptVoidRefundTests(_Base):
    def test_receipt_delete_reverses(self):
        self.open_shift(self.manager, '0')
        self.sell(self.manager, [(self.agel, '1')], total='200')
        r = self.api(self.manager).post('/api/finance/customer-receipts/', {
            'customer': str(self.customer.id), 'amount': '200', 'method': str(self.cash.id)}, format='json')
        self.assertEqual(self.bal(self.drawer), D('200'))
        d = self.api(self.owner).delete(f'/api/finance/customer-receipts/{r.data["id"]}/')
        self.assertEqual(d.status_code, 204, d.content)
        self.assertEqual(self.bal(self.drawer), D('0'))
        self.assertTrue(CustomerReceipt.all_objects.get(pk=r.data['id']).is_deleted)

    def test_void_reverses_and_refuses_when_the_cash_is_gone(self):
        self.open_shift(self.cashier, '0')
        inv = self.sell(self.cashier, [(self.cash, '60'), (self.card, '40')])
        self.assertEqual(self.bal(self.drawer), D('60'))
        r = self.api(self.manager).post(f'/api/finance/invoices/{inv.id}/void/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((self.bal(self.drawer), self.bal(ledger.unsorted_box(self.store))), (D('0'), D('0')))
        self.assertEqual(self.lines(kind=K.REVERSAL).count(), 2)
        # the drawer's cash went to the safe → voiding can't hand it back from the drawer
        inv2 = self.sell(self.cashier, [(self.cash, '100')])
        ledger.transfer(self.drawer, self.safe, '100')
        r = self.api(self.manager).post(f'/api/finance/invoices/{inv2.id}/void/')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'), r.content)
        inv2.refresh_from_db()
        self.assertEqual(inv2.status, 'POSTED')
        # the PATCH road to VOID reverses too
        ledger.transfer(self.safe, self.drawer, '100')
        r = self.api(self.manager).patch(f'/api/finance/invoices/{inv2.id}/', {'status': 'VOID'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.bal(self.drawer), D('0'))

    def test_refund_blocked_when_the_drawer_would_go_below_zero(self):
        self.open_shift(self.cashier, '0')
        inv = self.sell(self.cashier, [(self.cash, '100')])
        ledger.transfer(self.drawer, self.safe, '100')
        r = self.refund(self.manager, inv, '100')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'), r.content)
        self.assertEqual(RefundInvoice.all_objects.count(), 0)
        StockLevel.objects.get(variant=self.variant, branch=self.branch)   # stock untouched by the refused return
        self.assertEqual(StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity, D('99'))

    def test_card_part_of_a_refund_leaves_the_card_box_and_delete_puts_it_back(self):
        self.card.lands_in = self.bank
        self.card.save()
        self.open_shift(self.cashier, '0')
        inv = self.sell(self.cashier, [(self.cash, '600'), (self.card, '400')], total='1000', qty='2')
        r = self.refund(self.manager, inv, '500')                   # half back: cash first (600 cash taken)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((self.bal(self.drawer), self.bal(self.bank)), (D('100'), D('400')))
        r2 = self.refund(self.manager, inv, '500')                  # the rest: 100 cash + 400 card
        self.assertEqual(r2.status_code, 201, r2.content)
        self.assertEqual((self.bal(self.drawer), self.bal(self.bank)), (D('0'), D('0')))
        d = self.api(self.owner).delete(f'/api/finance/refunds/{r2.data["id"]}/')
        self.assertEqual(d.status_code, 204, d.content)
        self.assertEqual((self.bal(self.drawer), self.bal(self.bank)), (D('100'), D('400')))

    def test_store_credit_refund_writes_no_line(self):
        self.open_shift(self.cashier, '0')
        inv = self.sell(self.cashier, [(self.cash, '100')])
        n = MoneyMove.all_objects.count()
        r = self.refund(self.manager, inv, '100', method='STORE_CREDIT')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(MoneyMove.all_objects.count(), n)


class RepairDeskTests(_Base):
    def test_repair_done_cash_lands_in_the_drawer_and_return_takes_it_out(self):
        from services.models import Service
        self.open_shift(self.cashier, '0')
        job = Service.objects.create(store=self.store, client_name='Ali', receive_date=date.today(),
                                     cost=D('300'), created_by=self.owner)
        r = self.api(self.cashier).post(f'/api/services/{job.id}/done/', {'method': str(self.cash.id)}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(self.bal(self.drawer), D('300'))
        line = self.lines(kind=K.SALE_IN).get()
        self.assertEqual(line.shift.user, self.cashier)
        r = self.api(self.manager).post(f'/api/services/{job.id}/return/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(self.bal(self.drawer), D('0'))


class IsolationTests(_Base):
    def setUp(self):
        super().setUp()
        other_owner = User.objects.create_user(username='own399', password='x', role='OWNER')
        other = Store.objects.create(name='Other', store_code='399', owner=other_owner)
        other_owner.store = other
        other_owner.save(update_fields=['store'])
        Branch.objects.create(store=other, name='B', address=Address.objects.create(store=other, street_1='1', city='C'))
        StoreSettings.objects.update_or_create(store=other, defaults={'treasury_enabled': True})
        part = ledger.setup(other)
        self.foreign = ledger.open_box(part=part, name='Their safe', kind=MoneyBox.Kind.CASH, opening='500')
        self.foreign_drawer = MoneyBox.all_objects.get(store=other, kind=MoneyBox.Kind.DRAWER)

    def test_other_shops_boxes_refused_everywhere(self):
        r = self.expense(self.manager, '10', self.foreign)
        self.assertEqual((r.status_code, r.data['code']), (400, 'box_not_found'), r.content)
        r = self.api(self.manager).post('/api/finance/supplier-payments/', {
            'supplier': str(self.sup.id), 'amount': '10', 'payment_method': str(self.card.id),
            'paid_from': str(self.foreign.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'box_not_found'), r.content)
        r = self.api(self.owner).patch(f'/api/finance/payment-methods/{self.card.id}/',
                                       {'lands_in': str(self.foreign.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'box_not_found'), r.content)
        r = self.open_shift(self.cashier, '0', drawer=str(self.foreign_drawer.id))
        self.assertEqual((r.status_code, r.data['code']), (400, 'bad_drawer'), r.content)
        self.open_shift(self.cashier, '0')
        sid = WorkShift.all_objects.get(user=self.cashier).id
        self.sell(self.cashier, [(self.cash, '100')])
        r = self.api(self.cashier).post(f'/api/finance/shifts/{sid}/close/', {
            'counted_cash': '100', 'move_to_box': str(self.foreign.id), 'move_amount': '50'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'move_target_not_allowed'), r.content)
        self.assertEqual(ledger.balance(self.foreign), D('500'))
        self.assertEqual([x['id'] for x in self.api(self.manager).get('/api/finance/shifts/drawers/').data],
                         [str(self.drawer.id)])


class SwitchOffTests(_Base):
    TREASURY = False
    CODE = '302'

    def test_no_line_is_ever_written_with_the_switch_off(self):
        r = self.open_shift(self.manager, '10')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.data['ledger'], r.data['drawer'], r.data['book_balance']), ('LEGACY', None, None))
        inv = self.sell(self.manager, [(self.cash, '60'), (self.card, '40')])
        self.assertEqual(self.refund(self.manager, inv, '100').status_code, 201)
        inv2 = self.sell(self.manager, [(self.cash, '100')])
        self.assertEqual(self.api(self.manager).post(f'/api/finance/invoices/{inv2.id}/void/').status_code, 200)
        self.sell(self.manager, [(self.agel, '1')], total='50')
        r = self.api(self.manager).post('/api/finance/customer-receipts/', {
            'customer': str(self.customer.id), 'amount': '50', 'method': str(self.cash.id)}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        r = self.expense(self.manager, '30')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsNone(r.data['paid_from'])
        r = self.api(self.manager).post('/api/finance/supplier-payments/', {
            'supplier': str(self.sup.id), 'amount': '40', 'payment_method': str(self.cash.id)}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsNone(r.data['paid_from'])
        sid = WorkShift.all_objects.get(user=self.manager).id
        d = self.api(self.manager).get('/api/finance/shifts/drawer/').data
        c = self.api(self.manager).post(f'/api/finance/shifts/{sid}/close/', {'counted_cash': '1'}, format='json')
        self.assertEqual(c.status_code, 200, c.content)
        # the old formula, as before: 10 + 60 + 100 (the void doesn't touch it) + 50 − 60 (the refund's cash)
        self.assertEqual(D(c.data['expected_cash']), D('160'))
        self.assertEqual(D(d['expected_balance']), D('160'))
        self.assertEqual(MoneyMove.all_objects.count(), 0)
        self.assertEqual(MoneyBox.all_objects.count(), 0)
        r = self.api(self.manager).get('/api/finance/shifts/drawers/')
        self.assertEqual((r.status_code, r.data['code']), (403, 'treasury_off'))

    def test_trash_restore_still_works_for_a_record_that_never_moved_money(self):
        e = Expense.all_objects.create(store=self.store, branch=self.branch, category=self.cat, amount=D('20'),
                                       date=date.today(), is_deleted=True)
        sudo = User.objects.create_user(username='sudo302', password='x', is_superadmin=True)
        r = self.api(sudo).post('/api/admin/trash/restore/', {'model': 'expense', 'id': str(e.id)}, format='json')
        self.assertEqual(r.status_code, 200, r.content)


class ReviewFixTests(_Base):
    """surgeon-reviewer (s169c) fixes: move-takings target, the payer's drawer, trash restore, off→on corrections."""
    CODE = '303'

    def _close(self, user, sid, target, amount='50'):
        return self.api(user).post(f'/api/finance/shifts/{sid}/close/', {
            'counted_cash': '100', 'move_to_box': str(target.id), 'move_amount': amount}, format='json')

    def test_move_takings_target_rules(self):
        sid = self.open_shift(self.cashier, '0').data['id']
        self.sell(self.cashier, [(self.cash, '100')])
        wallet = ledger.open_box(part=self.part, name='Wallet', kind=MoneyBox.Kind.WALLET)
        for target in (self.bank, wallet, self.drawer, ledger.unsorted_box(self.store)):
            r = self._close(self.cashier, sid, target)
            self.assertEqual((r.status_code, r.data['code']), (400, 'move_target_not_allowed'), target.name)
        self.safe.is_active = False
        self.safe.save(update_fields=['is_active'])
        r = self._close(self.cashier, sid, self.safe)
        self.assertEqual((r.status_code, r.data['code']), (400, 'move_target_not_allowed'))
        self.safe.is_active = True
        self.safe.save(update_fields=['is_active'])
        self.assertEqual(WorkShift.all_objects.get(pk=sid).status, 'OPEN')
        r = self._close(self.cashier, sid, self.safe)                    # a cashier may move to the safe
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.bal(self.safe), D('1050'))
        # a manager may also move to the bank / an e-wallet
        sid = self.open_shift(self.manager, '50').data['id']
        self.sell(self.manager, [(self.cash, '50')], total='50')
        r = self._close(self.manager, sid, self.bank, amount='100')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((self.bal(self.bank), self.bal(self.drawer)), (D('100'), D('0')))

    def test_cash_refund_leaves_the_refunders_own_drawer_never_a_colleagues(self):
        b2 = Branch.objects.create(store=self.store, name='Second',
                                   address=Address.objects.create(store=self.store, street_1='2', city='C'))
        ledger.setup(self.store)
        drawer2 = ledger.default_drawer(b2)
        self.assertEqual(self.open_shift(self.cashier, '0').status_code, 201)
        inv = self.sell(self.cashier, [(self.cash, '100')])
        r = self.api(self.manager).post('/api/finance/shifts/', {'branch': str(b2.id), 'starting_cash': '100',
                                                                 'count_reason': 'float'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.refund(self.manager, inv, '40').status_code, 201)    # refund made in the sale's branch
        self.assertEqual((self.bal(self.drawer), self.bal(drawer2)), (D('100'), D('60')))

    def test_a_completed_sale_cannot_be_deleted_only_voided(self):
        inv = self.sell(self.cashier, [(self.cash, '100')])
        r = self.api(self.manager).delete(f'/api/finance/invoices/{inv.id}/')
        self.assertEqual((r.status_code, r.data['code']), (400, 'void_not_delete'))
        inv.refresh_from_db()
        self.assertFalse(inv.is_deleted)
        draft = self.draft()
        self.assertEqual(self.api(self.manager).delete(f'/api/finance/invoices/{draft.id}/').status_code, 204)

    def test_payment_lands_in_the_payers_drawer_in_any_branch(self):
        b2 = Branch.objects.create(store=self.store, name='Second',
                                   address=Address.objects.create(store=self.store, street_1='2', city='C'))
        ledger.setup(self.store)
        drawer2 = ledger.default_drawer(b2)
        r = self.api(self.cashier).post('/api/finance/shifts/', {'branch': str(b2.id), 'starting_cash': '0'},
                                        format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['drawer'], drawer2.id)
        inv = self.sell(self.cashier, [(self.cash, '100')])              # a sale of the FIRST branch
        r = self.api(self.cashier).post('/api/finance/payments/', {
            'invoice': str(self.sell(self.cashier, [(self.agel, '1')]).id), 'method': str(self.cash.id),
            'amount': '40'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(inv.branch_id, self.branch.id)
        self.assertEqual((self.bal(drawer2), self.bal(self.drawer)), (D('140'), D('0')))
        # no open shift → the sale's branch default drawer
        self.sell(self.manager, [(self.cash, '10')], total='10')
        self.assertEqual(self.bal(self.drawer), D('10'))

    def test_trash_restore_of_a_money_record_is_refused(self):
        sudo = User.objects.create_user(username='sudo303', password='x', is_superadmin=True)
        eid = self.expense(self.manager, '100', self.safe).data['id']
        self.assertEqual(self.api(self.owner).delete(f'/api/finance/expenses/{eid}/').status_code, 204)
        r = self.api(sudo).post('/api/admin/trash/restore/', {'model': 'expense', 'id': str(eid)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'restore_blocked_treasury'), r.content)
        self.assertTrue(Expense.all_objects.get(pk=eid).is_deleted)
        # even after the switch goes off, a row that had a box line stays deleted
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=False)
        r = self.api(sudo).post('/api/admin/trash/restore/', {'model': 'expense', 'id': str(eid)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'restore_blocked_treasury'), r.content)

    def test_switch_off_then_on_keeps_the_boxes_right(self):
        api, owner = self.api(self.manager), self.api(self.owner)
        e1 = self.expense(self.manager, '100', self.safe).data['id']
        e2 = self.expense(self.manager, '50', self.safe).data['id']
        self.assertEqual(self.bal(self.safe), D('850'))
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=False)
        self.assertEqual(owner.delete(f'/api/finance/expenses/{e1}/').status_code, 204)        # reversed while off
        r = api.patch(f'/api/finance/expenses/{e2}/', {'amount': '70'}, format='json')          # reversed, no new line
        self.assertEqual(r.status_code, 200, r.content)
        e3 = self.expense(self.manager, '10').data['id']                                        # off: no line at all
        self.assertEqual(self.bal(self.safe), D('1000'))
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=True)
        self.assertEqual(self.bal(self.safe), D('1000'))
        self.assertEqual(MoneyMove.all_objects.filter(expense_id=e3).count(), 0)
        # back on: the old rows stay out of the boxes; new ones go in
        api.patch(f'/api/finance/expenses/{e2}/', {'amount': '75'}, format='json')
        self.assertEqual(self.bal(self.safe), D('1000'))
        self.expense(self.manager, '1', self.safe)
        self.assertEqual(self.bal(self.safe), D('999'))

    def test_void_while_off_still_reverses_even_below_zero(self):
        self.open_shift(self.cashier, '0')
        inv = self.sell(self.cashier, [(self.cash, '100')])
        ledger.transfer(self.drawer, self.safe, '100')
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=False)
        r = self.api(self.manager).post(f'/api/finance/invoices/{inv.id}/void/')
        self.assertEqual(r.status_code, 200, r.content)                 # off: never blocked by the box floor
        self.assertEqual(self.bal(self.drawer), D('-100'))              # the book follows what really happened

    def test_move_targets_follow_the_close_rule_per_role(self):
        wallet = ledger.open_box(part=self.part, name='Wallet', kind=MoneyBox.Kind.WALLET)
        reserve = ledger.open_box(part=self.part, name='Set aside', kind=MoneyBox.Kind.RESERVED)
        closed = ledger.open_box(part=self.part, name='Old safe', kind=MoneyBox.Kind.CASH)
        closed.is_active = False
        closed.save(update_fields=['is_active'])
        r = self.api(self.cashier).get('/api/finance/shifts/move-targets/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual({x['id'] for x in r.data}, {str(self.safe.id), str(reserve.id)})
        self.assertNotIn('balance', r.data[0])                          # a cashier never sees what a box holds
        r = self.api(self.manager).get('/api/finance/shifts/move-targets/')
        self.assertEqual({x['id'] for x in r.data},
                         {str(self.safe.id), str(reserve.id), str(self.bank.id), str(wallet.id)})
        self.assertEqual({x['name']: x['balance'] for x in r.data}['Safe'], '1000.00')
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=False)
        r = self.api(self.cashier).get('/api/finance/shifts/move-targets/')
        self.assertEqual((r.status_code, r.data['code']), (403, 'treasury_off'))
