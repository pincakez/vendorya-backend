"""Treasury Wave 2 foundation (TREASURY.md § 12): the owner level's money rules — draws, put-ins, loans, amanat,
partners and settlements. The API tests live with the endpoints; these test the ledger doors themselves."""
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import Store, Branch, Address, StoreSettings
from finance.models import Expense, ExpenseCategory
from users.models import User
from treasury import ledger
from treasury.ledger import TreasuryError
from treasury.models import MoneyBox, MoneyMove, OwnerPart, Loan, PartEntry, Partner, PartnerSettlement

D = Decimal
K = MoneyMove.Kind


def _shop(code, owner, owner_level=True):
    store = Store.objects.create(name=f'Shop {code}', store_code=code, owner=owner)
    owner.store = store
    owner.save(update_fields=['store'])
    branch = Branch.objects.create(store=store, name=f'Main {code}',
                                   address=Address.objects.create(store=store, street_1='1', city='Cairo'))
    StoreSettings.objects.update_or_create(store=store, defaults={'treasury_enabled': True,
                                                                  'treasury_owner_level': owner_level})
    ledger.setup(store)
    if owner_level:
        ledger.setup_owner_level(store)
    return store, branch


class OwnerLevelLedgerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='w2_own', password='x', role='OWNER')
        self.store, self.branch = _shop('401', self.user)
        self.shop = ledger.shop_part(self.store)
        self.owner = self.shop.owner
        self.home = ledger.personal_part(self.owner)
        self.side = OwnerPart.all_objects.create(owner=self.owner, kind=OwnerPart.Kind.BUSINESS, name='Side')
        self.safe = ledger.open_box(part=self.shop, name='Safe', kind=MoneyBox.Kind.CASH, opening='1000')
        self.home_cash = ledger.open_box(part=self.home, name='Home cash', kind=MoneyBox.Kind.CASH, opening='200')
        self.side_cash = ledger.open_box(part=self.side, name='Side cash', kind=MoneyBox.Kind.CASH)

    # ── switch + who ──
    def test_switches_and_who_is_the_owner(self):
        self.assertTrue(ledger.owner_level_on(self.store))
        self.assertEqual(ledger.owner_for_user(self.user), self.owner)
        admin = User.objects.create_user(username='w2_adm', password='x', role='ADMIN', store=self.store)
        second_owner_login = User.objects.create_user(username='w2_own2', password='x', role='OWNER', store=self.store)
        self.assertIsNone(ledger.owner_for_user(admin))
        self.assertIsNone(ledger.owner_for_user(second_owner_login))   # same shop, not the person
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=False)
        self.assertFalse(ledger.owner_level_on(self.store))            # the owner level rides on the Treasury

    def test_one_personal_part_per_owner(self):
        self.assertEqual(ledger.setup_owner_level(self.store), self.home)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OwnerPart.all_objects.create(owner=self.owner, kind=OwnerPart.Kind.PERSONAL)

    def test_personal_and_business_boxes_never_reach_a_shop_query(self):
        shop_boxes = set(MoneyBox.all_objects.filter(store=self.store).values_list('id', flat=True))
        self.assertIn(self.safe.id, shop_boxes)
        self.assertNotIn(self.home_cash.id, shop_boxes)
        self.assertNotIn(self.side_cash.id, shop_boxes)
        self.assertIsNone(MoneyMove.all_objects.filter(box=self.home_cash).first().store_id)

    def test_owner_parts_hide_a_shop_whose_treasury_is_off(self):
        self.assertEqual(set(ledger.owner_parts(self.owner)), {self.shop, self.home, self.side})
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=False)
        self.assertEqual(set(ledger.owner_parts(self.owner)), {self.home, self.side})

    # ── draws / put-ins ──
    def test_shop_to_personal_is_a_draw_and_never_touches_the_pnl(self):
        from reports.views import compute_pnl
        today = timezone.localdate()
        before = compute_pnl(self.store, today, today)[1]
        out, inn, label = ledger.move_between(self.safe, self.home_cash, '300', user=self.user)
        self.assertEqual((out.kind, inn.kind, label), (K.DRAW_OUT, K.DRAW_IN, 'DRAW'))
        self.assertEqual(out.transfer_id, inn.transfer_id)
        self.assertEqual(ledger.balance(self.safe), D('700'))
        self.assertEqual(ledger.balance(self.home_cash), D('500'))
        self.assertEqual(compute_pnl(self.store, today, today)[1], before)

    def test_put_in_and_between_businesses(self):
        _o, _i, label = ledger.move_between(self.home_cash, self.side_cash, '50')
        self.assertEqual((_o.kind, _i.kind, label), (K.PUT_OUT, K.PUT_IN, 'PUT_IN'))
        _o, _i, label = ledger.move_between(self.safe, self.side_cash, '100')
        self.assertEqual((_o.kind, _i.kind, label), (K.DRAW_OUT, K.PUT_IN, 'BETWEEN'))
        bank = ledger.open_box(part=self.shop, name='Bank', kind=MoneyBox.Kind.BANK)
        _o, _i, label = ledger.move_between(self.safe, bank, '10')
        self.assertEqual(label, 'TRANSFER')

    def test_draw_above_cash_writes_nothing(self):
        n = MoneyMove.all_objects.count()
        with self.assertRaises(TreasuryError) as e:
            ledger.move_between(self.safe, self.home_cash, '1000.01')
        self.assertEqual(e.exception.code, 'not_enough_money')
        self.assertEqual(MoneyMove.all_objects.count(), n)

    def test_cross_owner_refused(self):
        other = User.objects.create_user(username='w2_oth', password='x', role='OWNER')
        ostore, _b = _shop('402', other)
        obox = ledger.open_box(part=ledger.shop_part(ostore), name='Theirs', kind=MoneyBox.Kind.CASH, opening='5')
        with self.assertRaises(TreasuryError) as e:
            ledger.move_between(self.safe, obox, '1')
        self.assertEqual(e.exception.code, 'other_owner')

    def test_draw_reversal_restores_both_boxes(self):
        out, inn, _l = ledger.move_between(self.safe, self.home_cash, '300')
        ledger.reverse(inn, reason='wrong')
        ledger.reverse(out, reason='wrong')
        self.assertEqual(ledger.balance(self.safe), D('1000'))
        self.assertEqual(ledger.balance(self.home_cash), D('200'))

    # ── loans ──
    def test_borrowed_loan_in_repay_closed_and_reversal_reopens(self):
        loan = ledger.loan_open(self.home, Loan.Direction.BORROWED, 'A lender', '500', self.home_cash)
        self.assertEqual(ledger.balance(self.home_cash), D('700'))
        self.assertEqual(ledger.loan_owed(loan), D('500'))
        with self.assertRaises(TreasuryError) as e:
            ledger.loan_pay(loan, '500.01', self.home_cash)
        self.assertEqual(e.exception.code, 'loan_overpaid')
        ledger.loan_pay(loan, '200', self.home_cash)
        last = ledger.loan_pay(loan, '300', self.home_cash)
        loan.refresh_from_db()
        self.assertEqual((ledger.loan_owed(loan), loan.is_closed), (D('0'), True))
        self.assertEqual(ledger.balance(self.home_cash), D('200'))
        ledger.reverse(last, reason='paid by mistake')
        loan.refresh_from_db()
        self.assertEqual((ledger.loan_owed(loan), loan.is_closed), (D('300'), False))

    def test_lent_loan_mirror(self):
        loan = ledger.loan_open(self.home, Loan.Direction.LENT, 'A friend', '150', self.home_cash)
        self.assertEqual(ledger.balance(self.home_cash), D('50'))
        self.assertEqual(ledger.loan_owed(loan), D('150'))
        ledger.loan_more(loan, '50', self.home_cash)
        self.assertEqual(ledger.loan_owed(loan), D('200'))
        ledger.loan_pay(loan, '200', self.home_cash)
        loan.refresh_from_db()
        self.assertTrue(loan.is_closed)
        self.assertEqual(ledger.balance(self.home_cash), D('200'))

    def test_loan_box_must_be_in_the_loan_part(self):
        with self.assertRaises(TreasuryError) as e:
            ledger.loan_open(self.home, Loan.Direction.BORROWED, 'X', '10', self.safe)
        self.assertEqual(e.exception.code, 'box_other_part')

    def test_lending_above_cash_refused(self):
        with self.assertRaises(TreasuryError):
            ledger.loan_open(self.home, Loan.Direction.LENT, 'X', '201', self.home_cash)
        self.assertFalse(Loan.objects.exists())   # the loan row rolled back with its line

    # ── amanat ──
    def test_amana_live_total_and_overdraw(self):
        a = ledger.amana_receive(self.shop, 'A neighbour', '400', self.safe)
        b = ledger.amana_receive(self.home, 'A cousin', '100', self.home_cash)
        self.assertEqual(ledger.amanat_total(self.owner), D('500'))
        with self.assertRaises(TreasuryError) as e:
            ledger.amana_return(a, '400.01', self.safe)
        self.assertEqual(e.exception.code, 'amana_overdrawn')
        ledger.amana_return(a, '400', self.safe)
        a.refresh_from_db()
        self.assertTrue(a.is_closed)
        self.assertEqual(ledger.amanat_total(self.owner), D('100'))
        self.assertEqual(ledger.amana_held(b), D('100'))

    # ── partners + settlements ──
    def _income(self, part, box, amount, direction=PartEntry.Direction.IN, owner_only=False, day=None):
        e = PartEntry.objects.create(owner=self.owner, part=part, direction=direction, amount=D(amount), box=box,
                                     owner_only=owner_only, date=day or self._day)
        sign = 1 if direction == PartEntry.Direction.IN else -1
        ledger.post(box, sign * D(amount), K.OTHER_INCOME_IN if sign > 0 else K.EXPENSE_OUT, part_entry=e,
                    check_floor=False)
        return e

    @property
    def _day(self):
        return timezone.localdate() - timedelta(days=1)

    def test_shares_capped_at_100(self):
        with transaction.atomic():
            ledger.check_shares(self.side, D('60'))
        Partner.objects.create(owner=self.owner, part=self.side, name='P1', share_pct=D('60'))
        with self.assertRaises(TreasuryError) as e:
            ledger.check_shares(self.side, D('40.01'))
        self.assertEqual(e.exception.code, 'share_total_too_high')
        with self.assertRaises(TreasuryError) as e:
            ledger.check_shares(self.home, D('1'))
        self.assertEqual(e.exception.code, 'partners_business_only')

    def test_settlement_maths_with_owner_only_expense(self):
        p = Partner.objects.create(owner=self.owner, part=self.side, name='P1', share_pct=D('30'))
        self._income(self.side, self.side_cash, '1200')
        self._income(self.side, self.side_cash, '200', PartEntry.Direction.OUT, owner_only=True)
        s = ledger.settle(self.side, self._day, self._day)
        self.assertEqual(s.profit, D('1000'))
        lines = {l.partner_id: l for l in s.lines.all()}
        self.assertEqual(lines[p.id].amount, D('360.00'))     # 30 % of (1000 + 200)
        self.assertEqual(lines[None].amount, D('640.00'))     # the owner carries the owner-only 200
        self.assertEqual(lines[None].share_pct, D('70'))
        self.assertEqual(ledger.partner_account(p), D('360.00'))

    def test_settlement_without_owner_only(self):
        p = Partner.objects.create(owner=self.owner, part=self.side, name='P1', share_pct=D('30'))
        self._income(self.side, self.side_cash, '1000')
        s = ledger.settle(self.side, self._day, self._day)
        self.assertEqual(s.lines.get(partner=p).amount, D('300.00'))

    def test_loss_is_shared_and_rounding_goes_to_the_owner(self):
        p1 = Partner.objects.create(owner=self.owner, part=self.side, name='P1', share_pct=D('33.33'))
        p2 = Partner.objects.create(owner=self.owner, part=self.side, name='P2', share_pct=D('33.33'))
        self._income(self.side, self.side_cash, '100', PartEntry.Direction.OUT)
        s = ledger.settle(self.side, self._day, self._day)
        amounts = {l.partner_id: l.amount for l in s.lines.all()}
        self.assertEqual(amounts[p1.id], D('-33.33'))
        self.assertEqual(amounts[p2.id], D('-33.33'))
        self.assertEqual(sum(amounts.values()), D('-100.00'))   # every cent accounted for

    def test_settlement_overlap_future_and_cancel(self):
        Partner.objects.create(owner=self.owner, part=self.side, name='P1', share_pct=D('50'))
        s = ledger.settle(self.side, self._day, self._day)
        with self.assertRaises(TreasuryError) as e:
            ledger.settle(self.side, self._day - timedelta(days=3), self._day)
        self.assertEqual(e.exception.code, 'settlement_overlap')
        with self.assertRaises(TreasuryError) as e:
            ledger.settle(self.side, self._day + timedelta(days=1), timezone.localdate() + timedelta(days=1))
        self.assertEqual(e.exception.code, 'settlement_future')
        ledger.cancel_settlement(s, reason='redo')
        self.assertEqual(ledger.settle(self.side, self._day, self._day).part, self.side)   # the period is free again

    def test_no_partners_and_personal_refused(self):
        with self.assertRaises(TreasuryError) as e:
            ledger.settle(self.side, self._day, self._day)
        self.assertEqual(e.exception.code, 'no_partners')
        with self.assertRaises(TreasuryError) as e:
            ledger.part_profit(self.home, self._day, self._day)
        self.assertEqual(e.exception.code, 'personal_no_pnl')

    def test_partner_account_settled_plus_put_in_minus_paid(self):
        p = Partner.objects.create(owner=self.owner, part=self.side, name='P1', share_pct=D('50'))
        self._income(self.side, self.side_cash, '400')
        ledger.settle(self.side, self._day, self._day)                    # +200
        ledger.partner_put_in(p, '100', self.side_cash)                   # +100
        pay = ledger.partner_pay(p, '250', self.side_cash)                # −250
        self.assertEqual(ledger.partner_account(p), D('50'))
        ledger.reverse(pay, reason='wrong amount')
        self.assertEqual(ledger.partner_account(p), D('300'))

    def test_shop_profit_uses_the_pnl_and_owner_only_expenses(self):
        cat = ExpenseCategory.objects.create(store=self.store, name='Rent')
        Expense.objects.create(store=self.store, branch=self.branch, category=cat, amount=D('80'), date=self._day,
                               owner_only=True)
        Expense.objects.create(store=self.store, branch=self.branch, category=cat, amount=D('20'), date=self._day)
        fig = ledger.part_profit(self.shop, self._day, self._day)
        self.assertEqual(fig, {'profit': D('-100.00'), 'owner_only_expenses': D('80')})
