"""Owner level API — part B (TREASURY.md § 12.4): partners, settlements, business P&L, the Expense `owner_only` flag and
shop-staff privacy on the Wave 1 statements. Money maths (the ledger doors) is tested in tests_owner.py; these test the
endpoints on top: gates, scoping, shapes and the numbers that reach the screen."""
import json
from datetime import timedelta
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, Branch, Address, StoreSettings, ActivityLog
from finance.models import Expense, ExpenseCategory, WorkShift
from users.models import User
from treasury import ledger
from treasury.models import (MoneyBox, MoneyMove, OwnerPart, Loan, OtherIncome, PartEntry, Partner, PartnerSettlement)

D = Decimal
K = MoneyMove.Kind
API = '/api/treasury/owner/'


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


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class PartnersApiTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.user = User.objects.create_user(username='tp_own', password='x', role='OWNER')
        self.store, self.branch = _shop('501', self.user)
        self.shop = ledger.shop_part(self.store)
        self.owner = self.shop.owner
        self.home = ledger.personal_part(self.owner)
        self.side = OwnerPart.all_objects.create(owner=self.owner, kind=OwnerPart.Kind.BUSINESS, name='Side business')
        self.safe = ledger.open_box(part=self.shop, name='Safe', kind=MoneyBox.Kind.CASH, opening='1000')
        self.home_cash = ledger.open_box(part=self.home, name='Personal box zq', kind=MoneyBox.Kind.CASH,
                                         opening='200')
        self.side_cash = ledger.open_box(part=self.side, name='Side cash', kind=MoneyBox.Kind.CASH)

        self.admin = User.objects.create_user(username='tp_adm', password='x', role='ADMIN', store=self.store)
        self.manager = User.objects.create_user(username='tp_mgr', password='x', role='MANAGER', store=self.store)
        self.owner2_login = User.objects.create_user(username='tp_own_b', password='x', role='OWNER', store=self.store)

        other = User.objects.create_user(username='tp_oth', password='x', role='OWNER')
        self.other_store, _b = _shop('502', other)
        oowner = ledger.shop_part(self.other_store).owner
        self.other_side = OwnerPart.all_objects.create(owner=oowner, kind=OwnerPart.Kind.BUSINESS, name='Theirs')
        self.other_box = ledger.open_box(part=self.other_side, name='Their cash', kind=MoneyBox.Kind.CASH, opening='9')
        self.other_partner = Partner.objects.create(owner=oowner, part=self.other_side, name='Their partner',
                                                    share_pct=D('10'))
        self.other_settlement = ledger.settle(self.other_side, self.day, self.day)

        self.c = _client(self.user)

    @property
    def day(self):
        return timezone.localdate() - timedelta(days=1)

    def _entry(self, part, box, amount, direction=PartEntry.Direction.IN, owner_only=False, category='', day=None):
        e = PartEntry.objects.create(owner=part.owner, part=part, direction=direction, amount=D(amount), box=box,
                                     owner_only=owner_only, category=category, date=day or self.day)
        sign = 1 if direction == PartEntry.Direction.IN else -1
        ledger.post(box, sign * D(amount), K.OTHER_INCOME_IN if sign > 0 else K.EXPENSE_OUT, part_entry=e,
                    check_floor=False)
        return e

    def _partner(self, part, pct, name='Partner one'):
        r = self.c.post(API + 'partners/', {'part': str(part.id), 'name': name, 'share_pct': str(pct)}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    # ── gates ───────────────────────────────────────────────────────────────────────────────────────────────────
    def test_gates(self):
        for client, code in ((_client(self.admin), None), (_client(self.manager), None),
                             (_client(self.owner2_login), 'not_owner')):
            for url in ('partners/', 'settlements/', f'pnl/?part={self.side.id}'):
                r = client.get(API + url)
                self.assertEqual(r.status_code, 403, url)
                if code:
                    self.assertEqual(r.data['code'], code)
        sudo = User.objects.create_user(username='tp_sudo', password='x', is_superadmin=True)
        self.assertEqual(_client(sudo).get(API + 'partners/').status_code, 403)
        StoreSettings.objects.filter(store=self.store).update(treasury_owner_level=False)
        r = self.c.get(API + 'partners/')
        self.assertEqual((r.status_code, r.data['code']), (403, 'owner_level_off'))
        StoreSettings.objects.filter(store=self.store).update(treasury_owner_level=True, treasury_enabled=False)
        r = self.c.get(API + 'settlements/')
        self.assertEqual((r.status_code, r.data['code']), (403, 'owner_level_off'))

    def test_another_owners_rows_are_404(self):
        p, s = self.other_partner, self.other_settlement
        self.assertEqual(self.c.get(API + f'partners/{p.id}/').status_code, 404)
        self.assertEqual(self.c.patch(API + f'partners/{p.id}/', {'name': 'x'}, format='json').status_code, 404)
        self.assertEqual(self.c.get(API + f'partners/{p.id}/statement/').status_code, 404)
        self.assertEqual(self.c.post(API + f'partners/{p.id}/pay/', {'amount': '1', 'box': str(self.other_box.id)},
                                     format='json').status_code, 404)
        self.assertEqual(self.c.post(API + f'settlements/{s.id}/cancel/', {'reason': 'x'}, format='json').status_code,
                         404)
        self.assertEqual(self.c.get(API + f'pnl/?part={self.other_side.id}').status_code, 404)
        self.assertEqual(self.c.post(API + 'partners/', {'part': str(self.other_side.id), 'name': 'X',
                                                         'share_pct': '5'}, format='json').status_code, 404)
        self.assertEqual(self.c.get(API + 'partners/').data, [])
        self.assertEqual(self.c.get(API + 'settlements/').data, [])
        # Their box for my partner = 404 (never revealed), and not a single line written.
        mine = self._partner(self.side, 10)
        n = MoneyMove.all_objects.count()
        r = self.c.post(API + f'partners/{mine["id"]}/put-in/', {'amount': '5', 'box': str(self.other_box.id)},
                        format='json')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(MoneyMove.all_objects.count(), n)
        self.other_settlement.refresh_from_db()
        self.assertIsNone(self.other_settlement.cancelled_at)

    # ── partners ────────────────────────────────────────────────────────────────────────────────────────────────
    def test_partner_create_list_and_shares_cap(self):
        p1 = self._partner(self.side, 60)
        self.assertEqual((p1['part'], p1['part_name'], p1['share_pct'], p1['account'], p1['is_active']),
                         (str(self.side.id), 'Side business', '60.00', '0.00', True))
        r = self.c.post(API + 'partners/', {'part': str(self.side.id), 'name': 'B', 'share_pct': '40.01'},
                        format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'share_total_too_high'))
        p2 = self._partner(self.side, 40, 'Partner two')
        r = self.c.patch(API + f'partners/{p1["id"]}/', {'share_pct': '61'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'share_total_too_high'))
        r = self.c.patch(API + f'partners/{p1["id"]}/', {'share_pct': '50', 'name': ' Partner one b '}, format='json')
        self.assertEqual((r.status_code, r.data['share_pct'], r.data['name']), (200, '50.00', 'Partner one b'))
        # Leaving is always allowed; coming back must fit under 100 %.
        r = self.c.patch(API + f'partners/{p1["id"]}/', {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['is_active']), (200, False))
        self._partner(self.side, 50, 'Partner three')
        r = self.c.patch(API + f'partners/{p1["id"]}/', {'is_active': True}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'share_total_too_high'))
        # The part is fixed once the partner exists.
        r = self.c.patch(API + f'partners/{p2["id"]}/', {'part': str(self.shop.id)}, format='json')
        self.assertEqual(r.status_code, 400)
        # List + filter.
        self.assertEqual(len(self.c.get(API + 'partners/').data), 3)
        self._partner(self.shop, 20, 'Shop partner')
        self.assertEqual(len(self.c.get(API + f'partners/?part={self.shop.id}').data), 1)
        self.assertEqual(self.c.get(API + 'partners/?part=nope').status_code, 400)

    def test_partner_validation_and_personal_refused(self):
        r = self.c.post(API + 'partners/', {'part': str(self.home.id), 'name': 'X', 'share_pct': '10'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'partners_business_only'))
        for pct in ('0', '100', '-5'):
            r = self.c.post(API + 'partners/', {'part': str(self.side.id), 'name': 'X', 'share_pct': pct},
                            format='json')
            self.assertEqual(r.status_code, 400, pct)
            self.assertIn('share_pct', r.data)
        r = self.c.post(API + 'partners/', {'part': str(self.side.id), 'name': '  ', 'share_pct': '5'}, format='json')
        self.assertIn('name', r.data)
        self.assertFalse(Partner.objects.filter(owner=self.owner).exists())

    # ── settlements ─────────────────────────────────────────────────────────────────────────────────────────────
    def _settle(self, part, df=None, dt=None):
        return self.c.post(API + 'settlements/', {'part': str(part.id), 'date_from': str(df or self.day),
                                                  'date_to': str(dt or self.day)}, format='json')

    def test_business_settle_30_percent(self):
        p = self._partner(self.side, 30)
        self._entry(self.side, self.side_cash, '1000')
        r = self._settle(self.side)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['profit'], r.data['owner_only_expenses']), ('1000.00', '0.00'))
        self.assertEqual([(l['partner'], l['partner_name'], l['share_pct'], l['amount']) for l in r.data['lines']],
                         [(p['id'], 'Partner one', '30.00', '300.00'), (None, None, '70.00', '700.00')])
        self.assertEqual(self.c.get(API + f'partners/{p["id"]}/').data['account'], '300.00')

    def test_business_settle_with_owner_only_expense(self):
        p = self._partner(self.side, 30)
        self._entry(self.side, self.side_cash, '1200')
        self._entry(self.side, self.side_cash, '200', PartEntry.Direction.OUT, owner_only=True)
        r = self._settle(self.side)
        self.assertEqual((r.data['profit'], r.data['owner_only_expenses']), ('1000.00', '200.00'))
        amounts = {l['partner']: l['amount'] for l in r.data['lines']}
        self.assertEqual((amounts[p['id']], amounts[None]), ('360.00', '640.00'))

    def test_shop_settle_uses_real_expenses_with_owner_only(self):
        p = self._partner(self.shop, 30)
        income = OtherIncome.objects.create(store=self.store, box=self.safe, amount=D('1200'), date=self.day)
        ledger.post(self.safe, D('1200'), K.OTHER_INCOME_IN, other_income=income)
        cat = ExpenseCategory.objects.create(store=self.store, name='Cat one')
        Expense.objects.create(store=self.store, branch=self.branch, category=cat, amount=D('200'), date=self.day,
                               owner_only=True)
        r = self._settle(self.shop)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['profit'], r.data['owner_only_expenses']), ('1000.00', '200.00'))
        amounts = {l['partner']: l['amount'] for l in r.data['lines']}
        self.assertEqual((amounts[p['id']], amounts[None]), ('360.00', '640.00'))

    def test_preview_saves_nothing_and_matches(self):
        p = self._partner(self.side, 30)
        self._entry(self.side, self.side_cash, '1200')
        self._entry(self.side, self.side_cash, '200', PartEntry.Direction.OUT, owner_only=True)
        r = self.c.get(API + f'settlements/preview/?part={self.side.id}&date_from={self.day}&date_to={self.day}')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['id'], r.data['profit'], r.data['owner_only_expenses'], r.data['base']),
                         (None, '1000.00', '200.00', '1200.00'))
        self.assertEqual([(l['partner'], l['amount']) for l in r.data['lines']],
                         [(p['id'], '360.00'), (None, '640.00')])
        self.assertFalse(PartnerSettlement.objects.filter(owner=self.owner).exists())
        # Bad / missing dates → 400 with a clear message; personal → 400.
        r = self.c.get(API + f'settlements/preview/?part={self.side.id}&date_from=2026-02-31&date_to={self.day}')
        self.assertEqual((r.status_code, r.data['date_from']), (400, 'Use a date like 2026-01-31.'))
        r = self.c.get(API + f'settlements/preview/?part={self.side.id}&date_from={self.day}')
        self.assertEqual(r.status_code, 400)
        self.assertIn('date_to', r.data)
        r = self.c.get(API + f'settlements/preview/?part={self.home.id}&date_from={self.day}&date_to={self.day}')
        self.assertEqual((r.status_code, r.data['code']), (400, 'personal_no_pnl'))

    def test_overlap_refused_cancel_frees_period_and_account(self):
        p = self._partner(self.side, 50)
        self._entry(self.side, self.side_cash, '400')
        s = self._settle(self.side).data
        r = self._settle(self.side, self.day - timedelta(days=5), self.day)
        self.assertEqual((r.status_code, r.data['code']), (400, 'settlement_overlap'))
        r = self._settle(self.side, self.day, timezone.localdate() + timedelta(days=1))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.get(API + f'partners/{p["id"]}/').data['account'], '200.00')

        r = self.c.post(API + f'settlements/{s["id"]}/cancel/', {'reason': ''}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'reason_required'))
        r = self.c.post(API + f'settlements/{s["id"]}/cancel/', {'reason': 'typed the wrong period'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNotNone(r.data['cancelled_at'])
        self.assertEqual(r.data['cancel_reason'], 'typed the wrong period')
        self.assertEqual(self.c.get(API + f'partners/{p["id"]}/').data['account'], '0.00')
        r = self.c.post(API + f'settlements/{s["id"]}/cancel/', {'reason': 'again'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'already_cancelled'))
        self.assertEqual(self._settle(self.side).status_code, 201)   # the period is free again
        listed = self.c.get(API + f'settlements/?part={self.side.id}').data
        self.assertEqual(len(listed), 2)                              # the cancelled one stays, marked
        self.assertEqual(sum(1 for x in listed if x['cancelled_at']), 1)

    def test_settle_refusals(self):
        r = self._settle(self.side)
        self.assertEqual((r.status_code, r.data['code']), (400, 'no_partners'))
        r = self._settle(self.home)
        self.assertEqual((r.status_code, r.data['code']), (400, 'personal_no_pnl'))
        r = self.c.post(API + 'settlements/', {'part': str(self.side.id), 'date_from': 'x', 'date_to': str(self.day)},
                        format='json')
        self.assertIn('date_from', r.data)

    # ── partner money + statement ───────────────────────────────────────────────────────────────────────────────
    def test_put_in_pay_statement_and_account(self):
        p = self._partner(self.side, 50)
        self._entry(self.side, self.side_cash, '400')                    # side cash 400
        self._settle(self.side)                                          # +200 owed to the partner
        r = self.c.post(API + f'partners/{p["id"]}/put-in/', {'amount': '100', 'box': str(self.side_cash.id),
                                                              'note': 'cash in'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['move']['kind'], r.data['move']['amount'], r.data['account']),
                         ('PARTNER_IN', '100.00', '300.00'))
        r = self.c.post(API + f'partners/{p["id"]}/pay/', {'amount': '250', 'box': str(self.side_cash.id)},
                        format='json')
        self.assertEqual((r.status_code, r.data['move']['amount'], r.data['account']), (201, '-250.00', '50.00'))
        self.assertEqual(ledger.balance(self.side_cash), D('250'))
        # The box must be in the partner's part; above the cash → nothing written.
        r = self.c.post(API + f'partners/{p["id"]}/pay/', {'amount': '1', 'box': str(self.safe.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'box_other_part'))
        r = self.c.post(API + f'partners/{p["id"]}/pay/', {'amount': '250.01', 'box': str(self.side_cash.id)},
                        format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'))
        r = self.c.post(API + f'partners/{p["id"]}/pay/', {'amount': '0', 'box': str(self.side_cash.id)},
                        format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(ledger.balance(self.side_cash), D('250'))

        pay = MoneyMove.all_objects.get(partner_id=p['id'], kind=K.PARTNER_OUT)
        ledger.reverse(pay, reason='wrong amount')
        r = self.c.get(API + f'partners/{p["id"]}/statement/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual([(l['kind'], l['amount'], l['running']) for l in r.data['lines']],
                         [('SETTLEMENT', '200.00', '200.00'), ('PUT_IN', '100.00', '300.00'),
                          ('PAID', '-250.00', '50.00'), ('REVERSAL', '250.00', '300.00')])
        self.assertEqual(r.data['account'], '300.00')
        self.assertEqual(r.data['partner']['account'], '300.00')
        self.assertTrue(r.data['lines'][2]['reversed'])
        self.assertEqual(r.data['lines'][0]['date'], self.day)

    def test_an_inactive_partner_can_still_be_paid(self):
        p = self._partner(self.side, 10)
        ledger.post(self.side_cash, D('50'), K.OPENING, check_floor=False)
        self.c.patch(API + f'partners/{p["id"]}/', {'is_active': False}, format='json')
        r = self.c.post(API + f'partners/{p["id"]}/pay/', {'amount': '20', 'box': str(self.side_cash.id)},
                        format='json')
        self.assertEqual((r.status_code, r.data['account']), (201, '-20.00'))

    # ── P&L per part ────────────────────────────────────────────────────────────────────────────────────────────
    def test_business_pnl(self):
        self._entry(self.side, self.side_cash, '1000', category='Sales')
        self._entry(self.side, self.side_cash, '200', category='Service')
        self._entry(self.side, self.side_cash, '300', PartEntry.Direction.OUT, category='Rent', owner_only=True)
        gone = self._entry(self.side, self.side_cash, '999', category='Sales')
        gone.delete()                                                    # soft — never counted
        self._entry(self.side, self.side_cash, '77', category='Sales', day=self.day - timedelta(days=40))   # out of range
        r = self.c.get(API + f'pnl/?part={self.side.id}&date_from={self.day}&date_to={self.day}')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['part_kind'], r.data['income'], r.data['expenses'], r.data['net'],
                          r.data['owner_only_expenses']),
                         ('BUSINESS', '1200.00', '300.00', '900.00', '300.00'))
        self.assertEqual(r.data['by_category'], [
            {'direction': 'IN', 'category': 'Sales', 'amount': '1000.00'},
            {'direction': 'IN', 'category': 'Service', 'amount': '200.00'},
            {'direction': 'OUT', 'category': 'Rent', 'amount': '300.00'}])
        # Default range = the last 30 days (like the shop report): the old entry stays out.
        self.assertEqual(self.c.get(API + f'pnl/?part={self.side.id}').data['income'], '1200.00')

    def test_shop_pnl_is_the_shop_report(self):
        from reports.views import compute_pnl
        income = OtherIncome.objects.create(store=self.store, box=self.safe, amount=D('150'), date=self.day)
        ledger.post(self.safe, D('150'), K.OTHER_INCOME_IN, other_income=income)
        r = self.c.get(API + f'pnl/?part={self.shop.id}&date_from={self.day}&date_to={self.day}')
        self.assertEqual(r.status_code, 200, r.data)
        report = self.c.get(f'/api/reports/pnl/?date_from={self.day}&date_to={self.day}').data
        self.assertEqual(r.data['totals'], report['totals'])
        self.assertEqual(r.data['totals']['net'], compute_pnl(self.store, self.day, self.day)[1]['net'])
        self.assertEqual(r.data['part_kind'], 'SHOP')

    def test_pnl_refusals(self):
        r = self.c.get(API + f'pnl/?part={self.home.id}')
        self.assertEqual((r.status_code, r.data['code']), (400, 'personal_no_pnl'))
        r = self.c.get(API + f'pnl/?part={self.side.id}&date_to=2026-13-01')
        self.assertEqual((r.status_code, r.data['date_to']), (400, 'Use a date like 2026-01-31.'))
        r = self.c.get(API + f'pnl/?part={self.side.id}&date_from=2026-05-02&date_to=2026-05-01')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.get(API + 'pnl/').status_code, 400)
        # A second shop of the same person: its figures need that shop's own sign-in.
        second, _b = _shop('503', self.user)
        self.user.store = self.store
        self.user.save(update_fields=['store'])
        part2 = ledger.shop_part(second)
        self.assertEqual(part2.owner, self.owner)
        r = self.c.get(API + f'pnl/?part={part2.id}')
        self.assertEqual((r.status_code, r.data['code']), (400, 'switch_shop'))

    def test_nothing_reaches_the_shop_activity_log(self):
        before = ActivityLog.objects.filter(store=self.store).count()
        p = self._partner(self.side, 10, 'Partner one zq')
        ledger.post(self.side_cash, D('50'), K.OPENING, check_floor=False)
        self.c.post(API + f'partners/{p["id"]}/pay/', {'amount': '20', 'box': str(self.side_cash.id)}, format='json')
        self.assertEqual(ActivityLog.objects.filter(store=self.store).count(), before)

    # ── finance Expense: owner_only ─────────────────────────────────────────────────────────────────────────────
    def test_expense_owner_only_round_trip_touches_no_box_line(self):
        cat = ExpenseCategory.objects.create(store=self.store, name='Cat one')
        mgr = _client(self.manager)
        body = {'branch': str(self.branch.id), 'category': str(cat.id), 'amount': '40', 'date': str(self.day),
                'paid_from': str(self.safe.id)}
        r = mgr.post('/api/finance/expenses/', body, format='json')
        self.assertEqual((r.status_code, r.data['owner_only']), (201, False))
        eid = r.data['id']
        lines = MoneyMove.all_objects.filter(expense_id=eid).count()
        self.assertEqual(lines, 1)
        # only the owner may flip owner_only (s170 review) — a manager is refused, see Wave2ReviewFixTests
        r = self.c.patch(f'/api/finance/expenses/{eid}/', {'owner_only': True}, format='json')
        self.assertEqual((r.status_code, r.data['owner_only']), (200, True))
        self.assertTrue(mgr.get(f'/api/finance/expenses/{eid}/').data['owner_only'])
        self.assertEqual(MoneyMove.all_objects.filter(expense_id=eid).count(), lines)
        self.assertEqual(ledger.balance(self.safe), D('960'))
        r = self.c.post('/api/finance/expenses/', {**body, 'owner_only': True}, format='json')
        self.assertTrue(Expense.objects.get(pk=r.data['id']).owner_only)


class Wave2ReviewFixTests(PartnersApiTests):
    """s170 batch review fixes (surgeon-reviewer on A + B + C)."""

    def test_only_the_owner_may_flip_owner_only(self):
        cat = ExpenseCategory.objects.create(store=self.store, name='Cat one')
        r = _client(self.manager).post('/api/finance/expenses/', {
            'branch': str(self.branch.id), 'category': str(cat.id), 'amount': '20', 'date': str(self.day),
            'paid_from': str(self.safe.id), 'owner_only': True}, format='json')
        self.assertEqual(r.status_code, 400, r.content[:300])
        self.assertIn('owner_only', r.data)
        r = self.c.post('/api/finance/expenses/', {
            'branch': str(self.branch.id), 'category': str(cat.id), 'amount': '20', 'date': str(self.day),
            'paid_from': str(self.safe.id), 'owner_only': True}, format='json')
        self.assertEqual(r.status_code, 201, r.content[:300])
        eid = r.data['id']
        # a manager editing the amount (form re-sends the same flag) is fine; clearing it is not
        mgr = _client(self.manager)
        self.assertEqual(mgr.patch(f'/api/finance/expenses/{eid}/', {'description': 'x', 'owner_only': True},
                                   format='json').status_code, 200)
        self.assertEqual(mgr.patch(f'/api/finance/expenses/{eid}/', {'owner_only': False},
                                   format='json').status_code, 400)
        self.assertTrue(Expense.objects.get(pk=eid).owner_only)

    def test_business_with_a_partner_balance_cannot_close(self):
        p = Partner.objects.get(pk=self._partner(self.side, '30')['id'])
        ledger.partner_put_in(p, '100', self.side_cash)
        ledger.move_between(self.side_cash, self.home_cash, '100')
        self.c.patch(f'{API}boxes/{self.side_cash.id}/', {'is_active': False}, format='json')
        r = self.c.patch(f'{API}parts/{self.side.id}/', {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data.get('code')), (400, 'part_not_empty'), r.content[:300])

    def test_today_cannot_be_settled(self):
        self._partner(self.side, '30')
        today = timezone.localdate()
        r = self.c.post(f'{API}settlements/', {'part': str(self.side.id), 'date_from': str(today),
                                                'date_to': str(today)}, format='json')
        self.assertEqual((r.status_code, r.data.get('code')), (400, 'settlement_future'), r.content[:300])

    def test_not_a_uuid_is_404_not_500(self):
        self.assertEqual(self.c.get(f'{API}boxes/not-a-uuid/statement/').status_code, 404)
        r = self.c.post(f'{API}moves/', {'from_box': 'x', 'to_box': 'y', 'amount': '1'}, format='json')
        self.assertIn(r.status_code, (400, 404))


class ShopStaffPrivacyTests(TestCase):
    """Owner-level lines in SHOP boxes, as shop staff read them (Wave 1 endpoints): a generic label only."""
    SECRETS = ('Personal box zq', 'Lender zq', 'Neighbour zq', 'Partner one zq', 'secret zq')

    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.user = User.objects.create_user(username='tq_own', password='x', role='OWNER')
        self.store, self.branch = _shop('511', self.user)
        self.shop = ledger.shop_part(self.store)
        self.owner = self.shop.owner
        self.home = ledger.personal_part(self.owner)
        self.safe = ledger.open_box(part=self.shop, name='Safe', kind=MoneyBox.Kind.CASH, opening='1000')
        self.home_cash = ledger.open_box(part=self.home, name='Personal box zq', kind=MoneyBox.Kind.CASH)
        self.manager = User.objects.create_user(username='tq_mgr', password='x', role='MANAGER', store=self.store)
        self.admin = User.objects.create_user(username='tq_adm', password='x', role='ADMIN', store=self.store)

        self.draw_out, self.draw_in, _l = ledger.move_between(self.safe, self.home_cash, '100', user=self.user,
                                                              note='secret zq')
        self.partner = Partner.objects.create(owner=self.owner, part=self.shop, name='Partner one zq',
                                              share_pct=D('10'))
        self.paid = ledger.partner_pay(self.partner, '50', self.safe, user=self.user, note='secret zq')
        self.loan = ledger.loan_open(self.shop, Loan.Direction.BORROWED, 'Lender zq', '300', self.safe,
                                     user=self.user, note='secret zq')
        ledger.amana_receive(self.shop, 'Neighbour zq', '40', self.safe, user=self.user, note='secret zq')
        ledger.reverse(self.draw_in, reason='secret zq')
        ledger.reverse(self.draw_out, reason='secret zq')

    def test_shop_statement_shows_generic_labels_only(self):
        r = APIClient()
        r.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.manager).access_token))
        resp = r.get(f'/api/treasury/boxes/{self.safe.id}/statement/')
        self.assertEqual(resp.status_code, 200)
        text = json.dumps(resp.json())
        for s in self.SECRETS:
            self.assertNotIn(s, text)
        refs = {(l['kind'], l['ref']['type'] if l['ref'] else None) for l in resp.data['lines']}
        self.assertTrue({('DRAW_OUT', 'owner'), ('PARTNER_OUT', 'partner'), ('LOAN_IN', 'loan'),
                         ('AMANA_IN', 'amana'), ('REVERSAL', 'owner')} <= refs, refs)
        for l in resp.data['lines']:
            if l['kind'] != 'OPENING':
                self.assertEqual((l['ref']['id'], l['ref']['label'], l['note'], l['reason']), (None, '', '', ''))
        self.assertEqual(resp.data['closing_balance'], '1290.00')     # 1000 − 100 + 100 − 50 + 300 + 40
        # The personal box is not in the shop's box list either.
        names = [b['name'] for b in r.get('/api/treasury/boxes/').data]
        self.assertNotIn('Personal box zq', names)

    def test_shift_drawer_hides_the_owners_words(self):
        drawer = ledger.default_drawer(self.branch)
        ledger.post(drawer, D('500'), K.OPENING, check_floor=False, at=timezone.now() - timedelta(hours=1))
        WorkShift.objects.create(store=self.store, branch=self.branch, user=self.manager, drawer=drawer,
                                 ledger=WorkShift.Ledger.BOX)
        ledger.move_between(drawer, self.home_cash, '60', user=self.user, note='secret zq')
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.manager).access_token))
        resp = c.get('/api/finance/shifts/drawer/')
        self.assertEqual(resp.status_code, 200)
        text = json.dumps(resp.json())
        for s in self.SECRETS:
            self.assertNotIn(s, text)
        self.assertIn('DRAW_OUT', [m['line_kind'] for m in resp.data['moves']])

    def test_wave1_reverse_refuses_owner_level_lines(self):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.admin).access_token))
        fresh_out, _in, _l = ledger.move_between(self.safe, self.home_cash, '10', user=self.user)
        loan_line = MoneyMove.all_objects.get(loan=self.loan)
        for line in (fresh_out, loan_line, self.paid):
            resp = c.post(f'/api/treasury/moves/{line.id}/reverse/', {'reason': 'x'}, format='json')
            self.assertEqual((resp.status_code, resp.data['code']), (400, 'reverse_at_source'), line.kind)
        # The personal leg is not even a shop line → 404.
        resp = c.post(f'/api/treasury/moves/{_in.id}/reverse/', {'reason': 'x'}, format='json')
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(MoneyMove.all_objects.filter(reverses=fresh_out).exists())
