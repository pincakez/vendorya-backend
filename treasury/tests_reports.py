"""Treasury Wave 3 reports (TREASURY.md § 8, New 9): balances on a date, movements by kind (a reversal counts under the
kind it cancels), owner draws + per-day average, amanat, partners — and who may see them (shop managers / the owner only,
never another shop or another owner)."""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from django.utils import timezone

from treasury import ledger
from treasury.models import Amana, MoneyBox, MoneyMove, OwnerPart, Partner
from treasury.tests_owner_api import _client, _shop
from users.models import User

D = Decimal
K = MoneyMove.Kind
SHOP = '/api/treasury/reports/'
OWN = '/api/treasury/owner/reports/'


class TreasuryReportTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.user = User.objects.create_user(username='rp_own', password='x', role='OWNER')
        self.store, self.branch = _shop('611', self.user)
        self.manager = User.objects.create_user(username='rp_mgr', password='x', role='MANAGER', store=self.store)
        self.cashier = User.objects.create_user(username='rp_csh', password='x', role='CASHIER', store=self.store)
        self.shop = ledger.shop_part(self.store)
        self.owner = self.shop.owner
        self.home = ledger.personal_part(self.owner)
        self.side = OwnerPart.all_objects.create(owner=self.owner, kind=OwnerPart.Kind.BUSINESS, name='Side business')
        self.drawer = ledger.default_drawer(self.branch)
        self.now = timezone.now()
        self.ten_days_ago = self.now - timedelta(days=10)
        self.safe = ledger.open_box(part=self.shop, name='Safe', kind=MoneyBox.Kind.CASH, opening='1000')
        MoneyMove.all_objects.filter(box=self.safe).first()   # opening written now
        self.home_cash = ledger.open_box(part=self.home, name='Home cash', kind=MoneyBox.Kind.CASH)
        self.side_cash = ledger.open_box(part=self.side, name='Side cash', kind=MoneyBox.Kind.CASH, opening='300')
        self.c = _client(self.user)
        self.m = _client(self.manager)

    def _d(self, days_ago=0):
        return (timezone.localdate() - timedelta(days=days_ago)).isoformat()

    # ── shop ────────────────────────────────────────────────────────────────────────────────────────────────────
    def test_balances_on_a_date_and_who_may_see(self):
        ledger.transfer(self.safe, self.drawer, '100', at=self.now)
        r = self.m.get(f'{SHOP}balances/?date={self._d()}')
        self.assertEqual(r.status_code, 200, r.content)
        bals = {b['name']: b['balance'] for b in r.data['boxes']}
        self.assertEqual((bals['Safe'], r.data['total']), ('900.00', '1000.00'))
        # yesterday the safe did not exist yet → not listed, total 0
        r = self.m.get(f'{SHOP}balances/?date={self._d(1)}')
        self.assertEqual((r.data['total'], [b['name'] for b in r.data['boxes'] if b['name'] == 'Safe']), ('0.00', []))
        self.assertEqual(_client(self.cashier).get(f'{SHOP}balances/').status_code, 403)
        self.assertEqual(self.m.get(f'{SHOP}balances/?date=2026-02-31').status_code, 400)
        # never the owner's personal / business boxes
        self.assertNotIn('Home cash', bals)
        self.assertNotIn('Side cash', bals)

    def test_movements_by_kind_counts_a_reversal_under_its_kind(self):
        from treasury.models import OtherIncome
        oi = OtherIncome.objects.create(store=self.store, box=self.safe, amount=D('50'))
        line = ledger.post(self.safe, D('50'), K.OTHER_INCOME_IN, other_income=oi)
        ledger.post(self.safe, D('-30'), K.EXPENSE_OUT)
        ledger.reverse(line, reason='typo')
        r = self.m.get(f'{SHOP}movements/?date_from={self._d()}&date_to={self._d()}')
        self.assertEqual(r.status_code, 200, r.content)
        kinds = {k['kind']: k for k in r.data['by_kind']}
        self.assertNotIn('REVERSAL', kinds)
        self.assertEqual((kinds['OTHER_INCOME_IN']['net'], kinds['OTHER_INCOME_IN']['count']), ('0.00', 0))
        self.assertEqual(kinds['EXPENSE_OUT']['money_out'], '30.00')
        self.assertEqual(kinds['OPENING']['money_in'], '1000.00')
        self.assertTrue(r.data['by_day'])
        # one box only + another shop's box is 404
        r = self.m.get(f'{SHOP}movements/?box={self.drawer.id}')
        self.assertEqual(r.data['by_box'], [])
        other = User.objects.create_user(username='rp_oth', password='x', role='OWNER')
        other_store, _ = _shop('612', other)
        theirs = ledger.open_box(part=ledger.shop_part(other_store), name='Theirs', kind=MoneyBox.Kind.CASH)
        self.assertEqual(self.m.get(f'{SHOP}movements/?box={theirs.id}').status_code, 404)

    # ── owner ───────────────────────────────────────────────────────────────────────────────────────────────────
    def test_owner_only(self):
        for path in ('balances/', 'movements/', 'draws/', 'amanat/', 'partners/'):
            self.assertEqual(self.c.get(OWN + path).status_code, 200, path)
            self.assertEqual(self.m.get(OWN + path).status_code, 403, path)

    def test_owner_balances_cover_every_part_and_name_the_amanat(self):
        ledger.amana_receive(self.home, 'A neighbour', '40', self.home_cash)
        r = self.c.get(OWN + 'balances/')
        names = {b['name']: b['balance'] for b in r.data['boxes']}
        self.assertEqual((names['Safe'], names['Home cash'], names['Side cash']), ('1000.00', '40.00', '300.00'))
        self.assertEqual((r.data['total'], r.data['amanat_held']), ('1340.00', '40.00'))

    def test_draws_per_part_with_per_day_average_and_reversed_left_out(self):
        ledger.move_between(self.safe, self.home_cash, '200', at=self.ten_days_ago)     # draw from the shop
        out, _inn, _ = ledger.move_between(self.side_cash, self.home_cash, '100')      # draw from the side business
        ledger.move_between(self.home_cash, self.side_cash, '50')                       # put back in
        wrong, _w, _ = ledger.move_between(self.safe, self.home_cash, '500')
        ledger.reverse(wrong, reason='wrong amount')
        r = self.c.get(f'{OWN}draws/?date_from={self._d(9)}&date_to={self._d()}')
        self.assertEqual(r.status_code, 200, r.content)
        # 10 days ago is outside: only the side draw (100) and the put-in (50) count; 10 days → 10.00 / day
        self.assertEqual((r.data['drawn'], r.data['put_in'], r.data['net_drawn'], r.data['days'], r.data['per_day']),
                         ('100.00', '50.00', '50.00', 10, '10.00'))
        r = self.c.get(f'{OWN}draws/?date_from={self._d(10)}&date_to={self._d()}')
        by = {p['name']: p for p in r.data['by_part']}
        self.assertEqual((r.data['drawn'], by[self.shop.name]['drawn'], by['Side business']['net_drawn']),
                         ('300.00', '200.00', '50.00'))

    def test_amanat_on_a_date_and_in_range(self):
        a = ledger.amana_receive(self.home, 'A neighbour', '80', self.home_cash)
        ledger.amana_return(a, '30', self.home_cash)
        r = self.c.get(f'{OWN}amanat/?date_from={self._d()}&date_to={self._d()}')
        row = r.data['amanat'][0]
        self.assertEqual((row['held'], row['received'], row['returned'], r.data['total']),
                         ('50.00', '80.00', '30.00', '50.00'))
        r = self.c.get(f'{OWN}amanat/?date={self._d(1)}')
        self.assertEqual(r.data['total'], '0.00')

    def test_partners(self):
        p = Partner.objects.create(owner=self.owner, part=self.side, name='Partner A', share_pct=D('40'))
        ledger.partner_put_in(p, '500', self.side_cash)
        ledger.partner_pay(p, '120', self.side_cash)
        r = self.c.get(OWN + 'partners/')
        row = r.data['partners'][0]
        self.assertEqual((row['name'], row['put_in'], row['paid'], row['balance_now']),
                         ('Partner A', '500.00', '120.00', '380.00'))
