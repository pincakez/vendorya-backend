"""Treasury Ledger foundation (TREASURY.md Wave 1): the engine's money rules."""
from decimal import Decimal

from django.db import connection, transaction, IntegrityError, InternalError
from django.test import TestCase

from core.models import Store, Branch, Address, StoreSettings
from users.models import User
from treasury import ledger
from treasury.ledger import TreasuryError
from treasury.models import MoneyBox, MoneyMove, Owner

D = Decimal


def _shop(code, owner):
    store = Store.objects.create(name=f'Shop {code}', store_code=code, owner=owner)
    owner.store = store
    owner.save(update_fields=['store'])
    branch = Branch.objects.create(store=store, name=f'Main {code}',
                                   address=Address.objects.create(store=store, street_1='1', city='Cairo'))
    return store, branch


class LedgerTests(TestCase):
    def setUp(self):
        self.owner_user = User.objects.create_user(username='t_own', password='x', role='OWNER')
        self.store, self.branch = _shop('201', self.owner_user)
        StoreSettings.objects.update_or_create(store=self.store, defaults={'treasury_enabled': True})
        self.part = ledger.setup(self.store)
        self.drawer = ledger.default_drawer(self.branch)
        self.safe = ledger.open_box(part=self.part, name='Safe', kind=MoneyBox.Kind.CASH, opening='1000')
        self.bank = ledger.open_box(part=self.part, name='Bank', kind=MoneyBox.Kind.BANK)

    # ── setup ──
    def test_switch_and_setup_are_idempotent(self):
        self.assertTrue(ledger.is_on(self.store))
        ledger.setup(self.store)
        self.assertEqual(MoneyBox.all_objects.filter(branch=self.branch, is_default_drawer=True).count(), 1)
        self.assertEqual(MoneyBox.all_objects.filter(store=self.store, system_role='UNSORTED').count(), 1)
        self.assertEqual(self.drawer.kind, MoneyBox.Kind.DRAWER)

    def test_one_person_with_two_shops_is_one_owner(self):
        store2, _b = _shop('202', self.owner_user)
        part2 = ledger.setup(store2)
        self.assertEqual(part2.owner_id, self.part.owner_id)
        self.assertEqual(Owner.objects.count(), 1)
        self.assertIsNone(self.part.owner.gid_subject)          # the GATES ID slot, empty until sign-in exists

    def test_switch_off_by_default(self):
        other = User.objects.create_user(username='t_own3', password='x', role='OWNER')
        store3, _b = _shop('203', other)
        self.assertFalse(ledger.is_on(store3))

    # ── balance + floor ──
    def test_opening_and_balance(self):
        self.assertEqual(ledger.balance(self.safe), D('1000'))
        self.assertEqual(MoneyMove.all_objects.get(box=self.safe).kind, MoneyMove.Kind.OPENING)

    def test_cash_box_never_below_zero(self):
        with self.assertRaises(TreasuryError) as e:
            ledger.post(self.safe, '-1000.01', MoneyMove.Kind.EXPENSE_OUT)
        self.assertEqual(e.exception.code, 'not_enough_money')
        ledger.post(self.safe, '-1000', MoneyMove.Kind.EXPENSE_OUT)
        self.assertEqual(ledger.balance(self.safe), D('0'))

    def test_bank_may_go_below_zero(self):
        ledger.post(self.bank, '-50', MoneyMove.Kind.EXPENSE_OUT)
        self.assertEqual(ledger.balance(self.bank), D('-50'))

    def test_zero_line_refused(self):
        with self.assertRaises(TreasuryError):
            ledger.post(self.safe, '0', MoneyMove.Kind.EXPENSE_OUT)

    # ── immutability ──
    def test_a_line_cannot_be_edited_or_deleted_by_the_app(self):
        m = MoneyMove.all_objects.get(box=self.safe)
        m.amount = D('5')
        with self.assertRaises(TypeError):
            m.save()
        with self.assertRaises(TypeError):
            m.delete()
        with self.assertRaises(TypeError):
            MoneyMove.all_objects.filter(pk=m.pk).update(amount=1)
        with self.assertRaises(TypeError):
            MoneyMove.all_objects.filter(pk=m.pk).delete()

    def test_the_database_refuses_edit_and_delete(self):
        m = MoneyMove.all_objects.get(box=self.safe)
        for sql in ('UPDATE treasury_moneymove SET amount = 1 WHERE id = %s',
                    'DELETE FROM treasury_moneymove WHERE id = %s'):
            with self.assertRaises((InternalError, IntegrityError, Exception)):
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(sql, [m.pk])
        self.assertEqual(ledger.balance(self.safe), D('1000'))

    # ── reverse ──
    def test_reverse_once_brings_the_balance_back(self):
        m = ledger.post(self.safe, '-300', MoneyMove.Kind.EXPENSE_OUT)
        with self.assertRaises(TreasuryError):
            ledger.reverse(m, reason='')                      # a reason is required
        r = ledger.reverse(m, reason='typed twice')
        self.assertEqual((r.kind, r.amount, r.reverses_id), (MoneyMove.Kind.REVERSAL, D('300'), m.pk))
        self.assertEqual(ledger.balance(self.safe), D('1000'))
        with self.assertRaises(TreasuryError):
            ledger.reverse(m, reason='again')
        with self.assertRaises(TreasuryError):
            ledger.reverse(r, reason='undo the undo')

    def test_reversing_income_already_spent_is_refused_on_a_cash_box(self):
        inn = ledger.post(self.drawer, '100', MoneyMove.Kind.SALE_IN)
        ledger.transfer(self.drawer, self.safe, '100')
        with self.assertRaises(TreasuryError):
            ledger.reverse(inn, reason='wrong sale')

    # ── transfer ──
    def test_transfer_is_two_lines_and_never_income(self):
        out, inn = ledger.transfer(self.safe, self.bank, '400', note='to the bank')
        self.assertEqual((out.amount, inn.amount), (D('-400'), D('400')))
        self.assertEqual(out.transfer_id, inn.transfer_id)
        self.assertEqual((ledger.balance(self.safe), ledger.balance(self.bank)), (D('600'), D('400')))

    def test_transfer_with_not_enough_money_writes_nothing(self):
        before = MoneyMove.all_objects.count()
        with self.assertRaises(TreasuryError):
            ledger.transfer(self.safe, self.bank, '1500')
        self.assertEqual(MoneyMove.all_objects.count(), before)

    def test_transfer_to_another_owners_box_refused(self):
        other = User.objects.create_user(username='t_own4', password='x', role='OWNER')
        store4, _b = _shop('204', other)
        theirs = ledger.open_box(part=ledger.setup(store4), name='Theirs', kind=MoneyBox.Kind.CASH)
        with self.assertRaises(TreasuryError) as e:
            ledger.transfer(self.safe, theirs, '10')
        self.assertEqual(e.exception.code, 'other_owner')

    # ── count ──
    def test_count_equal_writes_nothing(self):
        self.assertIsNone(ledger.count(self.safe, '1000'))

    def test_count_difference_needs_a_reason_and_may_go_below_book(self):
        with self.assertRaises(TreasuryError):
            ledger.count(self.safe, '900')
        m = ledger.count(self.safe, '900', reason='short, unknown')
        self.assertEqual((m.kind, m.amount), (MoneyMove.Kind.COUNT_ADJUST, D('-100')))
        self.assertEqual(ledger.balance(self.safe), D('900'))

    # ── where a payment lands ──
    def test_box_for_method(self):
        from finance.models import PaymentMethod
        cash = PaymentMethod.objects.create(store=self.store, name='Cash', is_cash=True)
        agel = PaymentMethod.objects.create(store=self.store, name='Ajel', is_agel=True)
        wallet = PaymentMethod.objects.create(store=self.store, name='Wallet')
        self.assertEqual(ledger.box_for_method(cash, branch=self.branch), self.drawer)
        self.assertIsNone(ledger.box_for_method(agel, branch=self.branch))
        self.assertEqual(ledger.box_for_method(wallet, branch=self.branch), ledger.unsorted_box(self.store))
        wallet.lands_in = self.bank
        wallet.save()
        self.assertEqual(ledger.box_for_method(wallet, branch=self.branch), self.bank)

    def test_one_open_shift_per_drawer(self):
        from finance.models import WorkShift
        u2 = User.objects.create_user(username='t_csh', password='x', role='CASHIER', store=self.store)
        WorkShift.objects.create(store=self.store, branch=self.branch, user=self.owner_user, drawer=self.drawer,
                                 ledger=WorkShift.Ledger.BOX)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WorkShift.objects.create(store=self.store, branch=self.branch, user=u2, drawer=self.drawer,
                                         ledger=WorkShift.Ledger.BOX)
