"""Treasury API (TREASURY.md § 11 part A): endpoints, roles, tenant isolation, the switch, statement maths, reversals,
other income and the P&L line."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, Branch, Address, StoreSettings, ActivityLog
from users.models import User
from treasury import ledger
from treasury.models import MoneyBox, MoneyMove, OtherIncome, IncomeCategory

D = Decimal
API = '/api/treasury/'


def _shop(code, owner, treasury=True):
    store = Store.objects.create(name=f'Shop {code}', store_code=code, owner=owner)
    owner.store = store
    owner.save(update_fields=['store'])
    branch = Branch.objects.create(store=store, name=f'Main {code}',
                                   address=Address.objects.create(store=store, street_1='1', city='Cairo'))
    StoreSettings.objects.update_or_create(store=store, defaults={'treasury_enabled': treasury})
    if treasury:
        ledger.setup(store)
    return store, branch


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


def _at(day, hour=12):
    return timezone.make_aware(datetime.combine(day, time(hour, 0)), timezone.get_current_timezone())


class TreasuryApiTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='ta_own', password='x', role='OWNER')
        self.store, self.branch = _shop('301', self.owner)
        self.admin = User.objects.create_user(username='ta_adm', password='x', role='ADMIN', store=self.store)
        self.manager = User.objects.create_user(username='ta_mgr', password='x', role='MANAGER', store=self.store)
        self.cashier = User.objects.create_user(username='ta_csh', password='x', role='CASHIER', store=self.store)
        self.part = ledger.shop_part(self.store)
        self.drawer = ledger.default_drawer(self.branch)
        self.safe = ledger.open_box(part=self.part, name='Safe', kind=MoneyBox.Kind.CASH, opening='1000')
        self.bank = ledger.open_box(part=self.part, name='Bank', kind=MoneyBox.Kind.BANK)

        self.other_owner = User.objects.create_user(username='ta_own2', password='x', role='OWNER')
        self.other_store, self.other_branch = _shop('302', self.other_owner)
        self.other_box = ledger.open_box(part=ledger.shop_part(self.other_store), name='Theirs',
                                         kind=MoneyBox.Kind.CASH, opening='50')

        self.c_own, self.c_adm = _client(self.owner), _client(self.admin)
        self.c_mgr, self.c_csh = _client(self.manager), _client(self.cashier)

    # ── the switch ──────────────────────────────────────────────────────────────────────────────────────────────
    def test_switch_on_through_settings_runs_setup_and_off_hides(self):
        own = User.objects.create_user(username='ta_own3', password='x', role='OWNER')
        store, branch = _shop('303', own, treasury=False)
        c = _client(own)
        r = c.get(API + 'boxes/')
        self.assertEqual((r.status_code, r.data['code']), (403, 'treasury_off'))
        self.assertFalse(MoneyBox.all_objects.filter(store=store).exists())

        r = c.patch('/api/core/settings/', {'treasury_enabled': True}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(MoneyBox.all_objects.filter(branch=branch, is_default_drawer=True).exists())
        self.assertTrue(MoneyBox.all_objects.filter(store=store, system_role='UNSORTED').exists())
        r = c.get(API + 'boxes/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 2)

        c.patch('/api/core/settings/', {'treasury_enabled': False}, format='json')
        r = c.get(API + 'boxes/')
        self.assertEqual((r.status_code, r.data['code']), (403, 'treasury_off'))
        self.assertEqual(MoneyBox.all_objects.filter(store=store).count(), 2)   # hidden, never deleted

    def test_switch_off_refuses_every_endpoint(self):
        StoreSettings.objects.filter(store=self.store).update(treasury_enabled=False)
        for method, url in (('get', 'boxes/'), ('post', 'transfers/'), ('get', 'income-categories/'),
                            ('get', 'other-income/'), ('get', f'boxes/{self.safe.id}/statement/')):
            r = getattr(self.c_own, method)(API + url, {}, format='json') if method == 'post' \
                else self.c_own.get(API + url)
            self.assertEqual((r.status_code, r.data.get('code')), (403, 'treasury_off'), url)

    def test_sudo_is_refused(self):
        sudo = User.objects.create_user(username='ta_sudo', password='x', is_superadmin=True)
        self.assertEqual(_client(sudo).get(API + 'boxes/').status_code, 403)

    # ── boxes ───────────────────────────────────────────────────────────────────────────────────────────────────
    def test_boxes_list_roles_and_balance(self):
        self.assertEqual(self.c_csh.get(API + 'boxes/').status_code, 403)
        r = self.c_mgr.get(API + 'boxes/')
        self.assertEqual(r.status_code, 200)
        by_id = {b['id']: b for b in r.data}
        self.assertEqual(set(by_id), {str(self.drawer.id), str(self.safe.id), str(self.bank.id),
                                      str(ledger.unsorted_box(self.store).id)})
        self.assertEqual(by_id[str(self.safe.id)]['balance'], '1000.00')
        self.assertEqual(r.data[0]['kind'], 'DRAWER')                       # drawers first
        self.assertEqual(r.data[0]['branch_name'], self.branch.name)
        self.assertEqual(set(r.data[0]), {'id', 'name', 'kind', 'system_role', 'branch', 'branch_name',
                                          'is_default_drawer', 'is_active', 'allow_negative', 'note', 'balance'})

    def test_list_is_one_query_for_balances(self):
        for i in range(5):
            ledger.open_box(part=self.part, name=f'B{i}', kind=MoneyBox.Kind.CASH, opening='10')
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            r = self.c_mgr.get(API + 'boxes/')
        self.assertEqual(len(r.data), 9)
        box_queries = [q for q in ctx.captured_queries if 'treasury_moneybox' in q['sql']]
        self.assertEqual(len(box_queries), 1)

    def test_only_the_owner_creates_boxes(self):
        body = {'name': 'Wallet', 'kind': 'WALLET', 'opening': '250.50', 'note': 'acct 12'}
        self.assertEqual(self.c_adm.post(API + 'boxes/', body, format='json').status_code, 403)
        self.assertEqual(self.c_mgr.post(API + 'boxes/', body, format='json').status_code, 403)
        r = self.c_own.post(API + 'boxes/', body, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['balance'], r.data['allow_negative'], r.data['note']), ('250.50', False, 'acct 12'))
        line = MoneyMove.all_objects.get(box_id=r.data['id'])
        self.assertEqual((line.kind, line.amount), (MoneyMove.Kind.OPENING, D('250.50')))
        self.assertTrue(ActivityLog.all_objects.filter(store=self.store, details__box_id=r.data['id']).exists())
        r = self.c_own.post(API + 'boxes/', {'name': 'Acct', 'kind': 'BANK'}, format='json')
        self.assertTrue(r.data['allow_negative'])                            # a bank may run an overdraft

    def test_box_create_validation(self):
        r = self.c_own.post(API + 'boxes/', {'name': 'Till 2', 'kind': 'DRAWER'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'drawer_needs_branch'))
        r = self.c_own.post(API + 'boxes/', {'name': 'Till 2', 'kind': 'DRAWER', 'branch': str(self.other_branch.id)},
                            format='json')
        self.assertEqual(r.status_code, 404)                                 # another shop's branch
        r = self.c_own.post(API + 'boxes/', {'name': 'X', 'kind': 'GOLD'}, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.c_own.post(API + 'boxes/', {'name': 'X', 'kind': 'CASH', 'opening': '-5'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'))
        r = self.c_own.post(API + 'boxes/', {'name': 'Till 2', 'kind': 'DRAWER', 'branch': str(self.branch.id)},
                            format='json')
        self.assertEqual((r.status_code, r.data['is_default_drawer'], r.data['branch']),
                         (201, False, self.branch.id))

    def test_patch_rename_and_close_rules(self):
        url = API + f'boxes/{self.safe.id}/'
        self.assertEqual(self.c_adm.patch(url, {'name': 'Office safe'}, format='json').status_code, 403)
        r = self.c_own.patch(url, {'name': 'Office safe', 'kind': 'BANK'}, format='json')
        self.assertEqual((r.status_code, r.data['name'], r.data['kind']), (200, 'Office safe', 'CASH'))
        r = self.c_own.patch(url, {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['code'], r.data['balance']), (400, 'box_not_empty', '1000.00'))
        ledger.transfer(self.safe, self.bank, '1000')
        r = self.c_own.patch(url, {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['is_active']), (200, False))
        unsorted = ledger.unsorted_box(self.store)
        r = self.c_own.patch(API + f'boxes/{unsorted.id}/', {'is_active': False}, format='json')
        self.assertEqual(r.data['code'], 'box_system')
        r = self.c_own.patch(API + f'boxes/{self.drawer.id}/', {'is_active': False}, format='json')
        self.assertEqual(r.data['code'], 'box_default_drawer')

    def test_drawer_with_open_shift_cannot_close(self):
        from finance.models import WorkShift
        till = ledger.open_box(part=self.part, name='Till 2', kind=MoneyBox.Kind.DRAWER, branch=self.branch)
        WorkShift.objects.create(store=self.store, branch=self.branch, user=self.cashier, drawer=till,
                                 ledger=WorkShift.Ledger.BOX)
        r = self.c_own.patch(API + f'boxes/{till.id}/', {'is_active': False}, format='json')
        self.assertEqual(r.data['code'], 'drawer_busy')

    def test_box_a_payment_method_lands_in_cannot_close(self):
        from finance.models import PaymentMethod
        card = PaymentMethod.objects.create(store=self.store, name='Card', lands_in=self.bank)
        r = self.c_own.patch(API + f'boxes/{self.bank.id}/', {'is_active': False}, format='json')
        self.assertEqual((r.status_code, r.data['code'], r.data['methods']), (400, 'box_in_use', 'Card'))
        card.lands_in = None
        card.save()
        r = self.c_own.patch(API + f'boxes/{self.bank.id}/', {'is_active': False}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_switch_on_refused_while_a_shift_is_open(self):
        from finance.models import WorkShift
        own = User.objects.create_user(username='ta_own5', password='x', role='OWNER')
        store, branch = _shop('305', own, treasury=False)
        shift = WorkShift.objects.create(store=store, branch=branch, user=own)
        c = _client(own)
        r = c.patch('/api/core/settings/', {'treasury_enabled': True}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'shifts_open'))
        self.assertFalse(MoneyBox.all_objects.filter(store=store).exists())
        WorkShift.objects.filter(pk=shift.pk).update(status=WorkShift.Status.CLOSED)
        self.assertEqual(c.patch('/api/core/settings/', {'treasury_enabled': True}, format='json').status_code, 200)

    # ── tenant isolation ────────────────────────────────────────────────────────────────────────────────────────
    def test_another_shops_box_is_invisible(self):
        oid = self.other_box.id
        self.assertNotIn(str(oid), [b['id'] for b in self.c_own.get(API + 'boxes/').data])
        self.assertEqual(self.c_own.get(API + f'boxes/{oid}/').status_code, 404)
        self.assertEqual(self.c_own.get(API + f'boxes/{oid}/statement/').status_code, 404)
        self.assertEqual(self.c_own.patch(API + f'boxes/{oid}/', {'name': 'x'}, format='json').status_code, 404)
        self.assertEqual(self.c_mgr.post(API + f'boxes/{oid}/count/', {'counted': '0', 'reason': 'x'},
                                         format='json').status_code, 404)
        r = self.c_own.post(API + 'transfers/', {'from_box': str(self.safe.id), 'to_box': str(oid), 'amount': '10'},
                            format='json')
        self.assertEqual(r.status_code, 404)
        their_line = MoneyMove.all_objects.get(box=self.other_box)
        self.assertEqual(self.c_own.post(API + f'moves/{their_line.id}/reverse/', {'reason': 'x'},
                                         format='json').status_code, 404)
        r = self.c_own.post(API + 'other-income/', {'box': str(oid), 'amount': '5'}, format='json')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(ledger.balance(self.other_box), D('50'))

    # ── statement ───────────────────────────────────────────────────────────────────────────────────────────────
    def test_statement_opening_running_and_totals_to_the_cent(self):
        box = ledger.open_box(part=self.part, name='Petty', kind=MoneyBox.Kind.CASH)
        d1 = timezone.localdate() - timedelta(days=3)
        d2, d3 = d1 + timedelta(days=1), d1 + timedelta(days=2)
        ledger.post(box, '100.10', MoneyMove.Kind.OPENING, at=_at(d1))
        ledger.post(box, '-30.05', MoneyMove.Kind.EXPENSE_OUT, at=_at(d2, 9))
        ledger.post(box, '12.34', MoneyMove.Kind.OTHER_INCOME_IN, at=_at(d2, 18))
        ledger.post(box, '-0.01', MoneyMove.Kind.EXPENSE_OUT, at=_at(d3))

        r = self.c_mgr.get(API + f'boxes/{box.id}/statement/', {'date_from': d2.isoformat(), 'date_to': d2.isoformat()})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['opening_balance'], '100.10')
        self.assertEqual([ln['amount'] for ln in r.data['lines']], ['-30.05', '12.34'])
        self.assertEqual([ln['running_balance'] for ln in r.data['lines']], ['70.05', '82.39'])
        self.assertEqual((r.data['total_in'], r.data['total_out'], r.data['closing_balance']),
                         ('12.34', '30.05', '82.39'))
        self.assertEqual(r.data['box']['id'], str(box.id))
        self.assertEqual(set(r.data['lines'][0]), {'id', 'box', 'at', 'kind', 'amount', 'running_balance', 'note',
                                                   'reason', 'created_by_name', 'ref', 'reversed', 'reverses'})

        r = self.c_mgr.get(API + f'boxes/{box.id}/statement/')
        self.assertEqual(r.data['opening_balance'], '0.00')
        self.assertEqual([ln['running_balance'] for ln in r.data['lines']], ['100.10', '70.05', '82.39', '82.38'])
        self.assertEqual(r.data['closing_balance'], str(ledger.balance(box)))

        self.assertEqual(self.c_csh.get(API + f'boxes/{box.id}/statement/').status_code, 403)
        r = self.c_mgr.get(API + f'boxes/{box.id}/statement/', {'date_from': '2026-02-31'})
        self.assertEqual(r.status_code, 400)

    def test_statement_refs(self):
        ledger.transfer(self.safe, self.bank, '200')
        r = self.c_mgr.get(API + f'boxes/{self.safe.id}/statement/')
        out = r.data['lines'][-1]
        self.assertEqual((out['kind'], out['ref']['type'], out['ref']['label']), ('TRANSFER_OUT', 'transfer', 'Bank'))
        opening = r.data['lines'][0]
        self.assertEqual(opening['ref'], {'type': 'opening', 'id': opening['id'], 'label': ''})
        from finance.models import ExpenseCategory, Expense, SalesInvoice, Payment, PaymentMethod
        from users.models import Customer
        cat = ExpenseCategory.objects.create(store=self.store, name='Rent')
        exp = Expense.objects.create(store=self.store, branch=self.branch, category=cat, amount=D('40'),
                                     date=timezone.localdate(), paid_from=self.safe)
        ledger.post(self.safe, '-40', MoneyMove.Kind.EXPENSE_OUT, expense=exp)
        r = self.c_mgr.get(API + f'boxes/{self.safe.id}/statement/')
        self.assertEqual(r.data['lines'][-1]['ref'], {'type': 'expense', 'id': str(exp.id), 'label': 'Rent'})

        inv = SalesInvoice.objects.create(store=self.store, branch=self.branch, date=timezone.now(),
                                          customer=Customer.objects.create(store=self.store, name='C', phone_number='01'))
        pay = Payment.objects.create(invoice=inv, amount=D('20'),
                                     method=PaymentMethod.objects.create(store=self.store, name='Cash', is_cash=True))
        ledger.post(self.drawer, '20', MoneyMove.Kind.SALE_IN, payment=pay)
        ledger.count(self.drawer, '19', reason='short')
        lines = self.c_mgr.get(API + f'boxes/{self.drawer.id}/statement/').data['lines']
        self.assertEqual(lines[0]['ref']['type'], 'sale')
        self.assertEqual(lines[0]['ref']['id'], str(inv.id))
        self.assertEqual(lines[1]['ref']['type'], 'count')
        rev = self.c_adm.post(API + f'moves/{lines[1]["id"]}/reverse/', {'reason': 'found it'}, format='json')
        self.assertEqual(rev.data['reversals'][0]['ref'], {'type': 'count', 'id': lines[1]['id'], 'label': ''})

    # ── count ───────────────────────────────────────────────────────────────────────────────────────────────────
    def test_count(self):
        url = API + f'boxes/{self.safe.id}/count/'
        self.assertEqual(self.c_csh.post(url, {'counted': '1000'}, format='json').status_code, 403)
        r = self.c_mgr.post(url, {'counted': '1000'}, format='json')
        self.assertEqual((r.data['difference'], r.data['move'], r.data['balance']), ('0.00', None, '1000.00'))
        r = self.c_mgr.post(url, {'counted': '900'}, format='json')
        self.assertEqual((r.status_code, r.data['code'], r.data['difference']), (400, 'reason_required', '-100.00'))
        r = self.c_mgr.post(url, {'counted': '900', 'reason': 'short, unknown'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['difference'], r.data['balance'], r.data['move']['kind']),
                         ('-100.00', '900.00', 'COUNT_ADJUST'))
        self.assertTrue(ActivityLog.all_objects.filter(store=self.store, details__move_id=r.data['move']['id']).exists())

    # ── transfers ───────────────────────────────────────────────────────────────────────────────────────────────
    def test_transfer_roles_and_result(self):
        body = {'from_box': str(self.safe.id), 'to_box': str(self.bank.id), 'amount': '400', 'note': 'deposit'}
        self.assertEqual(self.c_mgr.post(API + 'transfers/', body, format='json').status_code, 403)
        r = self.c_adm.post(API + 'transfers/', body, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['out']['amount'], r.data['in']['amount']), ('-400.00', '400.00'))
        self.assertEqual((r.data['out']['ref']['label'], r.data['in']['ref']['label']), ('Bank', 'Safe'))
        self.assertEqual((ledger.balance(self.safe), ledger.balance(self.bank)), (D('600'), D('400')))

    def test_transfer_not_enough_money_writes_nothing(self):
        before = MoneyMove.all_objects.count()
        r = self.c_adm.post(API + 'transfers/', {'from_box': str(self.safe.id), 'to_box': str(self.bank.id),
                                                  'amount': '1000.01'}, format='json')
        self.assertEqual((r.status_code, r.data['code'], r.data['balance']), (400, 'not_enough_money', '1000.00'))
        self.assertEqual(MoneyMove.all_objects.count(), before)

    # ── reverse ─────────────────────────────────────────────────────────────────────────────────────────────────
    def test_reverse_a_transfer_reverses_both_legs(self):
        out, inn = ledger.transfer(self.safe, self.bank, '300')
        url = API + f'moves/{inn.id}/reverse/'
        self.assertEqual(self.c_mgr.post(url, {'reason': 'wrong box'}, format='json').status_code, 403)
        r = self.c_adm.post(url, {'reason': ''}, format='json')
        self.assertEqual(r.data['code'], 'reason_required')
        r = self.c_adm.post(url, {'reason': 'wrong box'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(r.data['reversals']), 2)
        self.assertEqual((ledger.balance(self.safe), ledger.balance(self.bank)), (D('1000'), D('0')))
        st = self.c_mgr.get(API + f'boxes/{self.safe.id}/statement/').data['lines']
        self.assertTrue(next(ln for ln in st if ln['id'] == str(out.id))['reversed'])
        self.assertEqual(st[-1]['reverses'], str(out.id))
        r = self.c_adm.post(API + f'moves/{out.id}/reverse/', {'reason': 'again'}, format='json')
        self.assertEqual(r.data['code'], 'already_reversed')
        rev = MoneyMove.all_objects.get(reverses=out)
        r = self.c_adm.post(API + f'moves/{rev.id}/reverse/', {'reason': 'undo'}, format='json')
        self.assertEqual(r.data['code'], 'cannot_reverse_reversal')

    def test_reverse_opening_and_count_allowed_business_lines_refused(self):
        opening = MoneyMove.all_objects.get(box=self.safe, kind=MoneyMove.Kind.OPENING)
        r = self.c_adm.post(API + f'moves/{opening.id}/reverse/', {'reason': 'typed wrong'}, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(ledger.balance(self.safe), D('0'))
        sale_line = ledger.post(self.drawer, '75', MoneyMove.Kind.SALE_IN)
        r = self.c_adm.post(API + f'moves/{sale_line.id}/reverse/', {'reason': 'x'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (400, 'reverse_at_source'))
        self.assertEqual(ledger.balance(self.drawer), D('75'))

    # ── income categories ───────────────────────────────────────────────────────────────────────────────────────
    def test_income_categories_crud(self):
        self.assertEqual(self.c_csh.get(API + 'income-categories/').status_code, 403)
        r = self.c_mgr.post(API + 'income-categories/', {'name': ' Rent received '}, format='json')
        self.assertEqual((r.status_code, r.data['name']), (201, 'Rent received'))
        cid = r.data['id']
        r = self.c_mgr.patch(API + f'income-categories/{cid}/', {'name': 'Commission'}, format='json')
        self.assertEqual(r.data, {'id': cid, 'name': 'Commission'})
        self.assertEqual(self.c_mgr.delete(API + f'income-categories/{cid}/').status_code, 403)
        self.assertEqual(self.c_adm.delete(API + f'income-categories/{cid}/').status_code, 204)
        self.assertTrue(IncomeCategory.all_objects.get(pk=cid).is_deleted)
        self.assertEqual(self.c_mgr.get(API + 'income-categories/').data, [])
        other = IncomeCategory.objects.create(store=self.other_store, name='Theirs')
        self.assertEqual(self.c_mgr.get(API + f'income-categories/{other.id}/').status_code, 404)

    # ── other income ────────────────────────────────────────────────────────────────────────────────────────────
    def test_other_income_create_list_delete(self):
        cat = IncomeCategory.objects.create(store=self.store, name='Commission')
        body = {'box': str(self.bank.id), 'amount': '150.25', 'category': str(cat.id), 'description': 'agent fee',
                'branch': str(self.branch.id)}
        self.assertEqual(self.c_csh.post(API + 'other-income/', body, format='json').status_code, 403)
        r = self.c_mgr.post(API + 'other-income/', body, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['amount'], str(r.data['category']), str(r.data['box']), r.data['date']),
                         ('150.25', str(cat.id), str(self.bank.id), timezone.localdate().isoformat()))
        line = MoneyMove.all_objects.get(other_income_id=r.data['id'])
        self.assertEqual((line.kind, line.amount), (MoneyMove.Kind.OTHER_INCOME_IN, D('150.25')))
        self.assertEqual(ledger.balance(self.bank), D('150.25'))
        self.assertEqual([x['id'] for x in self.c_mgr.get(API + 'other-income/').data], [r.data['id']])

        url = API + f'other-income/{r.data["id"]}/'
        self.assertEqual(self.c_mgr.delete(url).status_code, 403)
        self.assertEqual(self.c_adm.delete(url, {'reason': 'typed twice'}, format='json').status_code, 204)
        self.assertTrue(OtherIncome.all_objects.get(pk=r.data['id']).is_deleted)
        self.assertEqual(ledger.balance(self.bank), D('0'))
        rev = MoneyMove.all_objects.get(reverses=line)
        self.assertEqual((rev.reason, rev.other_income_id), ('typed twice', line.other_income_id))   # keeps its source
        self.assertEqual(self.c_mgr.get(API + 'other-income/').data, [])

    def test_other_income_validation(self):
        r = self.c_mgr.post(API + 'other-income/', {'box': str(self.safe.id), 'amount': '0'}, format='json')
        self.assertEqual(r.status_code, 400)
        theirs = IncomeCategory.objects.create(store=self.other_store, name='Theirs')
        r = self.c_mgr.post(API + 'other-income/', {'box': str(self.safe.id), 'amount': '5',
                                                    'category': str(theirs.id)}, format='json')
        self.assertEqual(r.status_code, 404)
        self.assertFalse(OtherIncome.all_objects.filter(store=self.store).exists())

    def test_deleting_income_already_spent_from_cash_is_refused(self):
        r = self.c_mgr.post(API + 'other-income/', {'box': str(self.drawer.id), 'amount': '80'}, format='json')
        ledger.transfer(self.drawer, self.safe, '80')
        r = self.c_adm.delete(API + f'other-income/{r.data["id"]}/')
        self.assertEqual((r.status_code, r.data['code']), (400, 'not_enough_money'))
        self.assertEqual(OtherIncome.objects.filter(store=self.store).count(), 1)   # nothing changed

    # ── P&L ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def test_pnl_other_income_and_net(self):
        from finance.models import ExpenseCategory, Expense
        cat = ExpenseCategory.objects.create(store=self.store, name='Rent')
        Expense.objects.create(store=self.store, branch=self.branch, category=cat, amount=D('40'),
                               date=timezone.localdate())
        self.c_mgr.post(API + 'other-income/', {'box': str(self.bank.id), 'amount': '250'}, format='json')
        gone = self.c_mgr.post(API + 'other-income/', {'box': str(self.bank.id), 'amount': '99'}, format='json')
        self.c_adm.delete(API + f'other-income/{gone.data["id"]}/')       # deleted income never counts
        ledger.transfer(self.safe, self.bank, '500')                       # a transfer never appears
        t = self.c_mgr.get('/api/reports/pnl/').data['totals']
        self.assertEqual((t['other_income'], t['expenses'], t['net']), ('250.00', '40.00', '210.00'))
        self.assertTrue(t['reconciles'])
        rows = self.c_mgr.get('/api/reports/pnl/').data['rows']
        self.assertEqual(sum(D(r['other_income']) for r in rows), D('250'))
