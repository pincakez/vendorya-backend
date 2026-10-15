"""Owner level API — part A (TREASURY.md § 12.3 / § 12.6): the gate, tenant isolation, shop staff never seeing business /
personal money, parts, boxes, moving money (draw / put-in / between / transfer), reversals, entries, loans, amanat."""
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, Branch, Address, StoreSettings, ActivityLog
from users.models import User
from treasury import ledger
from treasury.models import MoneyBox, MoneyMove, OwnerPart, PartEntry, Loan, Amana

D = Decimal
K = MoneyMove.Kind
API = '/api/treasury/owner/'
SHOP_API = '/api/treasury/'


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


class OwnerApiTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.user = User.objects.create_user(username='oa_own', password='x', role='OWNER')
        self.store, self.branch = _shop('501', self.user)
        self.admin = User.objects.create_user(username='oa_adm', password='x', role='ADMIN', store=self.store)
        self.manager = User.objects.create_user(username='oa_mgr', password='x', role='MANAGER', store=self.store)
        self.second_owner = User.objects.create_user(username='oa_own2', password='x', role='OWNER', store=self.store)
        self.shop = ledger.shop_part(self.store)
        self.owner = self.shop.owner
        self.home = ledger.personal_part(self.owner)
        self.side = OwnerPart.all_objects.create(owner=self.owner, kind=OwnerPart.Kind.BUSINESS, name='Side business')
        self.drawer = ledger.default_drawer(self.branch)
        self.safe = ledger.open_box(part=self.shop, name='Safe', kind=MoneyBox.Kind.CASH, opening='1000')
        self.home_cash = ledger.open_box(part=self.home, name='Home cash', kind=MoneyBox.Kind.CASH, opening='200')
        self.side_cash = ledger.open_box(part=self.side, name='Side cash', kind=MoneyBox.Kind.CASH)

        # Another person, with their own shop + owner level and some money of every kind.
        self.other_user = User.objects.create_user(username='oa_oth', password='x', role='OWNER')
        self.other_store, self.other_branch = _shop('502', self.other_user)
        other_shop = ledger.shop_part(self.other_store)
        self.other_home = ledger.personal_part(other_shop.owner)
        self.other_box = ledger.open_box(part=self.other_home, name='Theirs', kind=MoneyBox.Kind.CASH, opening='500')
        self.other_loan = ledger.loan_open(self.other_home, Loan.Direction.BORROWED, 'A lender', '100', self.other_box)
        self.other_amana = ledger.amana_receive(self.other_home, 'A neighbour', '50', self.other_box)
        self.other_entry = ledger.entry_create(self.other_home, PartEntry.Direction.OUT, '10', self.other_box)
        self.other_line = MoneyMove.all_objects.filter(box=self.other_box, kind=K.OPENING).get()

        self.c = _client(self.user)

    def _bal(self, box):
        return ledger.balance(box)

    # ── the gate ────────────────────────────────────────────────────────────────────────────────────────────────
    def test_gate_refuses_everyone_but_the_person(self):
        self.assertEqual(self.c.get(API + 'overview/').status_code, 200)
        for user in (self.admin, self.manager):
            self.assertEqual(_client(user).get(API + 'overview/').status_code, 403)
        r = _client(self.second_owner).get(API + 'overview/')
        self.assertEqual((r.status_code, r.data['code']), (403, 'not_owner'))
        sudo = User.objects.create_user(username='oa_sudo', password='x', is_superadmin=True)
        self.assertEqual(_client(sudo).get(API + 'overview/').status_code, 403)

    def test_gate_owner_level_off_and_treasury_off(self):
        StoreSettings.objects.filter(store=self.store).update(treasury_owner_level=False)
        for url in ('overview/', 'parts/', 'boxes/', 'entries/', 'loans/', 'amanat/'):
            r = self.c.get(API + url)
            self.assertEqual((r.status_code, r.data['code']), (403, 'owner_level_off'), url)
        r = self.c.post(API + 'moves/', {}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (403, 'owner_level_off'))
        StoreSettings.objects.filter(store=self.store).update(treasury_owner_level=True, treasury_enabled=False)
        r = self.c.get(API + 'overview/')
        self.assertEqual((r.status_code, r.data['code']), (403, 'owner_level_off'))

    def test_settings_say_who_is_the_owner(self):
        self.assertTrue(self.c.get('/api/core/settings/').data['treasury_is_owner'])
        for user in (self.second_owner, self.admin, self.manager):
            self.assertFalse(_client(user).get('/api/core/settings/').data['treasury_is_owner'])
        StoreSettings.objects.filter(store=self.store).update(treasury_owner_level=False)
        self.assertFalse(self.c.get('/api/core/settings/').data['treasury_is_owner'])
        r = self.c.patch('/api/core/settings/', {'treasury_owner_level': True}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['treasury_is_owner'])

    # ── tenant: another owner's rows are never revealed ────────────────────────────────────────────────────────
    def test_another_owners_rows_are_404(self):
        c = self.c
        self.assertEqual(c.patch(API + f'parts/{self.other_home.id}/', {'name': 'x'}, format='json').status_code, 404)
        self.assertEqual(c.get(API + f'boxes/{self.other_box.id}/').status_code, 404)
        self.assertEqual(c.get(API + f'boxes/{self.other_box.id}/statement/').status_code, 404)
        self.assertEqual(c.post(API + f'boxes/{self.other_box.id}/count/', {'counted': '0', 'reason': 'x'},
                                format='json').status_code, 404)
        self.assertEqual(c.get(API + f'loans/{self.other_loan.id}/').status_code, 404)
        self.assertEqual(c.post(API + f'loans/{self.other_loan.id}/pay/',
                                {'amount': '10', 'box': str(self.home_cash.id)}, format='json').status_code, 404)
        self.assertEqual(c.get(API + f'amanat/{self.other_amana.id}/').status_code, 404)
        self.assertEqual(c.delete(API + f'entries/{self.other_entry.id}/').status_code, 404)
        self.assertEqual(c.post(API + f'lines/{self.other_line.id}/reverse/', {'reason': 'x'},
                                format='json').status_code, 404)
        # Their box as a source / target / home for a new row
        n = MoneyMove.all_objects.count()
        self.assertEqual(c.post(API + 'moves/', {'from_box': str(self.other_box.id), 'to_box': str(self.home_cash.id),
                                                 'amount': '10'}, format='json').status_code, 404)
        self.assertEqual(c.post(API + 'boxes/', {'part': str(self.other_home.id), 'name': 'x', 'kind': 'CASH'},
                                format='json').status_code, 404)
        self.assertEqual(c.post(API + 'loans/', {'part': str(self.home.id), 'direction': 'LENT', 'counterparty': 'x',
                                                 'amount': '10', 'box': str(self.other_box.id)},
                                format='json').status_code, 404)
        self.assertEqual(MoneyMove.all_objects.count(), n)
        # …and none of it is listed
        ids = {b['id'] for b in c.get(API + 'boxes/').data}
        self.assertNotIn(str(self.other_box.id), ids)
        self.assertEqual(c.get(API + 'loans/').data, [])
        self.assertEqual(c.get(API + 'amanat/').data, [])
        self.assertEqual(c.get(API + 'entries/').data, [])

    def test_shop_staff_never_see_business_or_personal_boxes(self):
        private = {str(self.home_cash.id), str(self.side_cash.id)}
        for user in (self.user, self.admin, self.manager):
            c = _client(user)
            ids = {b['id'] for b in c.get(SHOP_API + 'boxes/').data}
            self.assertFalse(ids & private, user.username)
            self.assertEqual(c.get(SHOP_API + f'boxes/{self.home_cash.id}/statement/').status_code, 404)
        line = MoneyMove.all_objects.filter(box=self.home_cash).first()
        self.assertEqual(_client(self.admin).post(SHOP_API + f'moves/{line.id}/reverse/', {'reason': 'x'},
                                                  format='json').status_code, 404)
        self.assertEqual(_client(self.admin).post(SHOP_API + 'transfers/', {
            'from_box': str(self.home_cash.id), 'to_box': str(self.safe.id), 'amount': '1'},
            format='json').status_code, 404)

    # ── overview ────────────────────────────────────────────────────────────────────────────────────────────────
    def test_overview_parts_boxes_and_totals(self):
        ledger.loan_open(self.home, Loan.Direction.BORROWED, 'A lender', '300', self.home_cash)
        ledger.amana_receive(self.side, 'A neighbour', '40', self.side_cash)
        r = self.c.get(API + 'overview/')
        self.assertEqual(r.status_code, 200, r.data)
        kinds = [p['kind'] for p in r.data['parts']]
        self.assertEqual(kinds, ['PERSONAL', 'SHOP', 'BUSINESS'])
        by_kind = {p['kind']: p for p in r.data['parts']}
        self.assertEqual(by_kind['PERSONAL']['total'], '500.00')                 # 200 + 300 borrowed
        self.assertEqual(by_kind['BUSINESS']['total'], '40.00')
        self.assertEqual(by_kind['SHOP']['total'], '1000.00')                    # drawer 0 + safe 1000 + unsorted 0
        self.assertIn(str(self.safe.id), {b['id'] for b in by_kind['SHOP']['boxes']})
        self.assertEqual(by_kind['SHOP']['store'], self.store.id)
        self.assertEqual(r.data['personal_total'], '500.00')
        self.assertEqual(r.data['business_total'], '1040.00')
        self.assertEqual(r.data['amanat_held'], '40.00')
        self.assertEqual(r.data['loans_owed_by_me'], '300.00')
        self.assertEqual(r.data['loans_owed_to_me'], '0.00')

    def test_overview_hides_another_shop_of_mine_whose_treasury_is_off(self):
        store2, _ = _shop('503', User.objects.create_user(username='oa_tmp', password='x', role='OWNER'))
        part2 = ledger.shop_part(store2)
        part2.owner = self.owner
        part2.save(update_fields=['owner'])
        MoneyBox.all_objects.filter(part=part2).update(owner=self.owner)
        kinds = [p['id'] for p in self.c.get(API + 'overview/').data['parts']]
        self.assertIn(str(part2.id), kinds)
        StoreSettings.objects.filter(store=store2).update(treasury_enabled=False)
        self.assertNotIn(str(part2.id), [p['id'] for p in self.c.get(API + 'overview/').data['parts']])

    # ── parts ───────────────────────────────────────────────────────────────────────────────────────────────────
    def test_parts_create_rename_close(self):
        r = self.c.post(API + 'parts/', {'name': 'Second business'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['kind'], r.data['is_active'], r.data['store']), ('BUSINESS', True, None))
        new_id = r.data['id']
        r = self.c.post(API + 'parts/', {'name': 'x', 'kind': 'PERSONAL'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'part_business_only'))
        self.assertEqual(self.c.post(API + 'parts/', {'name': '  '}, format='json').status_code, 400)

        r = self.c.patch(API + f'parts/{self.home.id}/', {'name': 'My pocket'}, format='json')
        self.assertEqual((r.status_code, r.data['name']), (200, 'My pocket'))
        r = self.c.patch(API + f'parts/{self.home.id}/', {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'part_fixed'))
        for body in ({'name': 'x'}, {'is_active': False}):
            r = self.c.patch(API + f'parts/{self.shop.id}/', body, format='json')
            self.assertEqual((r.status_code, r.data['code']), (400, 'part_fixed'))

        # A business with an open box can't close; once the box is closed, it can (and is gone from the list)
        r = self.c.patch(API + f'parts/{self.side.id}/', {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'part_not_empty'))
        self.assertEqual(self.c.patch(API + f'boxes/{self.side_cash.id}/', {'is_active': False},
                                      format='json').status_code, 200)
        r = self.c.patch(API + f'parts/{self.side.id}/', {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['is_active']), (200, False))
        ids = {p['id'] for p in self.c.get(API + 'parts/').data}
        self.assertEqual(ids, {str(self.home.id), str(self.shop.id), new_id})

    def test_part_with_an_open_loan_cannot_close(self):
        box = ledger.open_box(part=self.side, name='Bank', kind=MoneyBox.Kind.BANK)
        ledger.loan_open(self.side, Loan.Direction.BORROWED, 'A lender', '100', box)
        ledger.move_between(box, self.home_cash, '100')
        for b in (box, self.side_cash):
            self.assertEqual(self.c.patch(API + f'boxes/{b.id}/', {'is_active': False}, format='json').status_code,
                             200)
        r = self.c.patch(API + f'parts/{self.side.id}/', {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'part_not_empty'))

    # ── boxes ───────────────────────────────────────────────────────────────────────────────────────────────────
    def test_boxes_list_across_parts(self):
        r = self.c.get(API + 'boxes/')
        self.assertEqual(r.status_code, 200)
        rows = {b['id']: b for b in r.data}
        self.assertEqual(rows[str(self.home_cash.id)]['part_kind'], 'PERSONAL')
        self.assertEqual(rows[str(self.home_cash.id)]['balance'], '200.00')
        self.assertIsNone(rows[str(self.home_cash.id)]['store'])
        self.assertEqual(rows[str(self.safe.id)]['part_kind'], 'SHOP')
        self.assertEqual(rows[str(self.side_cash.id)]['part_name'], 'Side business')
        only_side = self.c.get(API + f'boxes/?part={self.side.id}').data
        self.assertEqual([b['id'] for b in only_side], [str(self.side_cash.id)])
        self.assertEqual(self.c.get(API + f'boxes/?part={self.other_home.id}').status_code, 404)

    def test_box_create_rules(self):
        r = self.c.post(API + 'boxes/', {'part': str(self.home.id), 'name': 'Savings', 'kind': 'SAVINGS',
                                         'opening': '75'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['balance'], r.data['part_kind'], r.data['store']), ('75.00', 'PERSONAL', None))
        box = MoneyBox.all_objects.get(pk=r.data['id'])
        self.assertEqual((box.owner_id, box.part_id, box.store_id), (self.owner.id, self.home.id, None))

        r = self.c.post(API + 'boxes/', {'part': str(self.home.id), 'name': 'D', 'kind': 'DRAWER'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'drawer_needs_shop'))
        r = self.c.post(API + 'boxes/', {'part': str(self.shop.id), 'name': 'D', 'kind': 'DRAWER'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'drawer_needs_branch'))
        r = self.c.post(API + 'boxes/', {'part': str(self.shop.id), 'name': 'D', 'kind': 'DRAWER',
                                         'branch': str(self.other_branch.id)}, format='json')
        self.assertEqual(r.status_code, 404)
        r = self.c.post(API + 'boxes/', {'part': str(self.home.id), 'name': 'B', 'kind': 'CASH',
                                         'branch': str(self.branch.id)}, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.c.post(API + 'boxes/', {'part': str(self.shop.id), 'name': 'Drawer 2', 'kind': 'DRAWER',
                                         'branch': str(self.branch.id)}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(MoneyBox.all_objects.get(pk=r.data['id']).store_id, self.store.id)
        # The shop's staff see a shop box made here; the personal one stays invisible to them
        ids = {b['id'] for b in _client(self.manager).get(SHOP_API + 'boxes/').data}
        self.assertIn(r.data['id'], ids)
        self.assertNotIn(str(box.id), ids)

    def test_box_close_rules_are_wave1(self):
        r = self.c.patch(API + f'boxes/{self.home_cash.id}/', {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'box_not_empty'))
        r = self.c.patch(API + f'boxes/{self.drawer.id}/', {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'box_default_drawer'))
        r = self.c.patch(API + f'boxes/{self.side_cash.id}/', {'name': 'Side till', 'note': 'n'}, format='json')
        self.assertEqual((r.status_code, r.data['name'], r.data['note']), (200, 'Side till', 'n'))
        r = self.c.patch(API + f'boxes/{self.side_cash.id}/', {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['is_active']), (200, False))

    def test_statement_and_count_of_a_personal_box(self):
        ledger.move_between(self.safe, self.home_cash, '300', user=self.user)
        r = self.c.get(API + f'boxes/{self.home_cash.id}/statement/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['opening_balance'], r.data['total_in'], r.data['total_out'],
                          r.data['closing_balance']), ('0.00', '500.00', '0.00', '500.00'))
        kinds = [(l['kind'], l['running_balance'], l['ref']['type'] if l['ref'] else None, l['ref'] and l['ref']['label'])
                 for l in r.data['lines']]
        self.assertEqual(kinds, [('OPENING', '200.00', 'opening', ''), ('DRAW_IN', '500.00', 'transfer', 'Safe')])
        self.assertEqual(r.data['box']['part_kind'], 'PERSONAL')
        # The safe's statement (the owner's view) names the personal box
        r = self.c.get(API + f'boxes/{self.safe.id}/statement/')
        self.assertEqual(r.data['lines'][-1]['ref']['label'], 'Home cash')
        r = self.c.get(API + f'boxes/{self.home_cash.id}/statement/?date_from=2026-02-31')
        self.assertEqual(r.status_code, 400)
        today = timezone.localdate().isoformat()
        r = self.c.get(API + f'boxes/{self.home_cash.id}/statement/?date_from={today}&date_to={today}')
        self.assertEqual((r.status_code, len(r.data['lines'])), (200, 2))

        r = self.c.post(API + f'boxes/{self.home_cash.id}/count/', {'counted': '480'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'reason_required'))
        r = self.c.post(API + f'boxes/{self.home_cash.id}/count/', {'counted': '480', 'reason': 'Short'},
                        format='json')
        self.assertEqual((r.status_code, r.data['difference'], r.data['balance']), (200, '-20.00', '480.00'))

    # ── moving money ────────────────────────────────────────────────────────────────────────────────────────────
    def test_draw_via_api_two_lines_and_the_shop_pnl_unchanged(self):
        from reports.views import compute_pnl
        today = timezone.localdate()
        before = compute_pnl(self.store, today, today)[1]
        n = MoneyMove.all_objects.count()
        r = self.c.post(API + 'moves/', {'from_box': str(self.safe.id), 'to_box': str(self.home_cash.id),
                                         'amount': '300', 'note': 'For home'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['kind'], r.data['out']['kind'], r.data['in']['kind']), ('DRAW', 'DRAW_OUT', 'DRAW_IN'))
        self.assertEqual((r.data['out']['amount'], r.data['in']['amount']), ('-300.00', '300.00'))
        self.assertEqual((r.data['out']['ref']['label'], r.data['in']['ref']['label']), ('Home cash', 'Safe'))
        self.assertEqual(MoneyMove.all_objects.count(), n + 2)
        self.assertEqual((self._bal(self.safe), self._bal(self.home_cash)), (D('700'), D('500')))
        self.assertEqual(compute_pnl(self.store, today, today)[1], before)
        # The shop's log names the shop box only — never the personal box
        logs = list(ActivityLog.objects.filter(store=self.store).values_list('action', flat=True))
        self.assertTrue(any('Safe' in a for a in logs))
        self.assertFalse(any('Home cash' in a for a in logs))

    def test_draw_above_balance_writes_nothing(self):
        n = MoneyMove.all_objects.count()
        r = self.c.post(API + 'moves/', {'from_box': str(self.home_cash.id), 'to_box': str(self.side_cash.id),
                                         'amount': '5000'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'))
        self.assertEqual(MoneyMove.all_objects.count(), n)

    def test_put_in_between_and_transfer_kinds(self):
        second = ledger.open_box(part=self.home, name='Wallet', kind=MoneyBox.Kind.WALLET)
        for src, dst, label, kinds in (
                (self.home_cash, self.side_cash, 'PUT_IN', ('PUT_OUT', 'PUT_IN')),
                (self.side_cash, self.safe, 'BETWEEN', ('DRAW_OUT', 'PUT_IN')),
                (self.home_cash, second, 'TRANSFER', ('TRANSFER_OUT', 'TRANSFER_IN'))):
            r = self.c.post(API + 'moves/', {'from_box': str(src.id), 'to_box': str(dst.id), 'amount': '10'},
                            format='json')
            self.assertEqual(r.status_code, 201, r.data)
            self.assertEqual((r.data['kind'], r.data['out']['kind'], r.data['in']['kind']), (label,) + kinds)
        r = self.c.post(API + 'moves/', {'from_box': str(second.id), 'to_box': str(second.id), 'amount': '1'},
                        format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'same_box'))

    # ── reversals ───────────────────────────────────────────────────────────────────────────────────────────────
    def test_reverse_a_draw_reverses_both_legs(self):
        out, inn, _ = ledger.move_between(self.safe, self.home_cash, '300', user=self.user)
        r = self.c.post(API + f'lines/{out.id}/reverse/', {'reason': 'Wrong box'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(r.data['reversals']), 2)
        self.assertEqual(r.data['reversals'][0]['box'], str(self.home_cash.id))   # receiving leg first
        self.assertEqual((self._bal(self.safe), self._bal(self.home_cash)), (D('1000'), D('200')))
        r = self.c.post(API + f'lines/{inn.id}/reverse/', {'reason': 'Again'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'already_reversed'))
        rev = MoneyMove.all_objects.filter(reverses=out).get()
        r = self.c.post(API + f'lines/{rev.id}/reverse/', {'reason': 'x'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'cannot_reverse_reversal'))

    def test_reverse_needs_a_reason_and_business_lines_are_refused(self):
        opening = MoneyMove.all_objects.get(box=self.home_cash, kind=K.OPENING)
        r = self.c.post(API + f'lines/{opening.id}/reverse/', {}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'reason_required'))
        entry = ledger.entry_create(self.home, PartEntry.Direction.OUT, '20', self.home_cash, category='Food')
        line = MoneyMove.all_objects.get(part_entry=entry)
        r = self.c.post(API + f'lines/{line.id}/reverse/', {'reason': 'x'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'reverse_at_source'))
        # A shop's other income line is undone where it was made too
        r = _client(self.user).post(SHOP_API + 'other-income/', {'box': str(self.safe.id), 'amount': '5'},
                                    format='json')
        self.assertEqual(r.status_code, 201, r.data)
        line = MoneyMove.all_objects.get(other_income_id=r.data['id'])
        r = self.c.post(API + f'lines/{line.id}/reverse/', {'reason': 'x'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'reverse_at_source'))
        jar = ledger.open_box(part=self.home, name='Jar', kind=MoneyBox.Kind.CASH, opening='50')
        jar_opening = MoneyMove.all_objects.get(box=jar, kind=K.OPENING)
        r = self.c.post(API + f'lines/{jar_opening.id}/reverse/', {'reason': 'Typed twice'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self._bal(jar), D('0'))

    # ── loans ───────────────────────────────────────────────────────────────────────────────────────────────────
    def test_borrowed_loan_create_pay_overpay_close_and_reverse(self):
        r = self.c.post(API + 'loans/', {'part': str(self.home.id), 'direction': 'BORROWED',
                                         'counterparty': 'A lender', 'amount': '1000', 'box': str(self.home_cash.id)},
                        format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['owed'], r.data['is_closed'], len(r.data['lines'])), ('1000.00', False, 1))
        self.assertEqual(r.data['lines'][0]['ref'], {'type': 'loan', 'id': r.data['id'], 'label': 'A lender'})
        lid = r.data['id']
        self.assertEqual(self._bal(self.home_cash), D('1200'))

        r = self.c.post(API + f'loans/{lid}/pay/', {'amount': '400', 'box': str(self.home_cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['owed']), (201, '600.00'))
        r = self.c.post(API + f'loans/{lid}/pay/', {'amount': '700', 'box': str(self.home_cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'loan_overpaid'))
        r = self.c.post(API + f'loans/{lid}/pay/', {'amount': '600', 'box': str(self.home_cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['owed'], r.data['is_closed']), (201, '0.00', True))
        self.assertEqual(len(r.data['lines']), 3)
        # The box must be of the loan's part
        r = self.c.post(API + f'loans/{lid}/more/', {'amount': '5', 'box': str(self.side_cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'box_other_part'))
        # Reversing the last payment brings the debt back
        last = MoneyMove.all_objects.filter(loan_id=lid, kind=K.LOAN_REPAY).order_by('-at', '-created_at').first()
        r = self.c.post(API + f'lines/{last.id}/reverse/', {'reason': 'Bounced'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        r = self.c.get(API + f'loans/{lid}/')
        self.assertEqual((r.data['owed'], r.data['is_closed'], len(r.data['lines'])), ('600.00', False, 4))
        self.assertEqual(r.data['lines'][-1]['reverses'], str(last.id))
        lst = self.c.get(API + 'loans/?open=1').data
        self.assertEqual([(x['id'], x['owed']) for x in lst], [(lid, '600.00')])
        r = self.c.get(API + 'overview/')
        self.assertEqual(r.data['loans_owed_by_me'], '600.00')

    def test_lent_loan_mirror_and_a_reversal_that_would_go_below_zero(self):
        r = self.c.post(API + 'loans/', {'part': str(self.home.id), 'direction': 'LENT', 'counterparty': 'A friend',
                                         'amount': '150', 'box': str(self.home_cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['owed']), (201, '150.00'))
        lid = r.data['id']
        self.assertEqual(self._bal(self.home_cash), D('50'))
        r = self.c.post(API + f'loans/{lid}/pay/', {'amount': '100', 'box': str(self.home_cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['owed']), (201, '50.00'))
        self.assertEqual(self.c.get(API + 'overview/').data['loans_owed_to_me'], '50.00')
        # Reversing the money lent now would leave them owing −100 → refused, nothing written
        start = MoneyMove.all_objects.get(loan_id=lid, kind=K.LOAN_OUT)
        n = MoneyMove.all_objects.count()
        r = self.c.post(API + f'lines/{start.id}/reverse/', {'reason': 'x'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'loan_overpaid'))
        self.assertEqual(MoneyMove.all_objects.count(), n)
        r = self.c.post(API + f'loans/{lid}/pay/', {'amount': '50', 'box': str(self.home_cash.id)}, format='json')
        self.assertEqual((r.data['owed'], r.data['is_closed']), ('0.00', True))
        r = self.c.post(API + 'loans/', {'part': str(self.home.id), 'direction': 'LENT', 'counterparty': ' ',
                                         'amount': '1', 'box': str(self.home_cash.id)}, format='json')
        self.assertEqual(r.status_code, 400)

    # ── amanat ──────────────────────────────────────────────────────────────────────────────────────────────────
    def test_amana_receive_return_above_held_refused_and_closed(self):
        r = self.c.post(API + 'amanat/', {'part': str(self.home.id), 'held_for': 'A neighbour', 'amount': '500',
                                          'box': str(self.home_cash.id)}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['held'], r.data['lines'][0]['ref']['label']), ('500.00', 'A neighbour'))
        aid = r.data['id']
        r = self.c.post(API + f'amanat/{aid}/more/', {'amount': '100', 'box': str(self.home_cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['held']), (201, '600.00'))
        self.assertEqual(self.c.get(API + 'overview/').data['amanat_held'], '600.00')
        r = self.c.post(API + f'amanat/{aid}/return/', {'amount': '601', 'box': str(self.home_cash.id)},
                        format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'amana_overdrawn'))
        r = self.c.post(API + f'amanat/{aid}/return/', {'amount': '600', 'box': str(self.home_cash.id)},
                        format='json')
        self.assertEqual((r.status_code, r.data['held'], r.data['is_closed']), (201, '0.00', True))
        self.assertEqual(self._bal(self.home_cash), D('200'))
        self.assertEqual(self.c.get(API + f'amanat/{aid}/').data['held'], '0.00')
        self.assertEqual(self.c.get(API + 'amanat/?open=1').data, [])

    # ── entries ─────────────────────────────────────────────────────────────────────────────────────────────────
    def test_entry_create_writes_its_line_and_delete_reverses_it(self):
        r = self.c.post(API + 'entries/', {'part': str(self.side.id), 'direction': 'IN', 'category': 'Consulting',
                                           'amount': '150', 'box': str(self.side_cash.id), 'owner_only': True},
                        format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['amount'], r.data['box_name'], r.data['part_kind']), ('150.00', 'Side cash', 'BUSINESS'))
        eid = r.data['id']
        line = MoneyMove.all_objects.get(part_entry_id=eid)
        self.assertEqual((line.kind, line.amount, line.box_id), (K.OTHER_INCOME_IN, D('150'), self.side_cash.id))
        self.assertEqual(self._bal(self.side_cash), D('150'))
        st = self.c.get(API + f'boxes/{self.side_cash.id}/statement/').data
        self.assertEqual(st['lines'][-1]['ref'], {'type': 'entry', 'id': eid, 'label': 'Consulting'})

        r = self.c.post(API + 'entries/', {'part': str(self.home.id), 'direction': 'OUT', 'category': 'Food',
                                           'amount': '30', 'box': str(self.home_cash.id), 'owner_only': True},
                        format='json')
        self.assertEqual((r.status_code, r.data['owner_only']), (201, False))   # owner-only means nothing in personal
        self.assertEqual(self._bal(self.home_cash), D('170'))
        self.assertEqual(len(self.c.get(API + f'entries/?part={self.side.id}').data), 1)
        self.assertEqual(len(self.c.get(API + 'entries/?direction=OUT').data), 1)
        self.assertEqual(self.c.get(API + 'entries/?date_from=nope').status_code, 400)

        r = self.c.delete(API + f'entries/{eid}/')
        self.assertEqual(r.status_code, 204)
        self.assertTrue(PartEntry.all_objects.get(pk=eid).is_deleted)
        self.assertEqual(self._bal(self.side_cash), D('0'))
        self.assertTrue(MoneyMove.all_objects.filter(reverses=line).exists())
        self.assertEqual(self.c.delete(API + f'entries/{eid}/').status_code, 404)

    def test_entry_rules(self):
        n = MoneyMove.all_objects.count()
        r = self.c.post(API + 'entries/', {'part': str(self.shop.id), 'direction': 'IN', 'amount': '10',
                                           'box': str(self.safe.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'entries_not_for_shop'))
        r = self.c.post(API + 'entries/', {'part': str(self.side.id), 'direction': 'IN', 'amount': '10',
                                           'box': str(self.home_cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'box_other_part'))
        r = self.c.post(API + 'entries/', {'part': str(self.side.id), 'direction': 'OUT', 'amount': '10',
                                           'box': str(self.side_cash.id)}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'))
        r = self.c.post(API + 'entries/', {'part': str(self.side.id), 'direction': 'IN', 'amount': '0',
                                           'box': str(self.side_cash.id)}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(MoneyMove.all_objects.count(), n)
        self.assertFalse(PartEntry.objects.filter(part=self.side).exists())

    def test_deleting_income_already_spent_is_refused(self):
        entry = ledger.entry_create(self.side, PartEntry.Direction.IN, '100', self.side_cash)
        ledger.move_between(self.side_cash, self.home_cash, '100')
        r = self.c.delete(API + f'entries/{entry.id}/')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'))
        self.assertFalse(PartEntry.all_objects.get(pk=entry.pk).is_deleted)
