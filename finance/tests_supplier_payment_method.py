"""PROLINE-PARITY Upgrade 6: supplier payments use the shop's OWN payment methods (the till's list) instead of
the fixed CASH / BANK / CARD / OTHER code. Covers the API (own method, other shop's, Ajel, store credit,
soft-deleted, old clients sending `method`), the legacy-code mapping + data migration, the statement and the
supplier balance."""
import importlib
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from django.apps import apps as live_apps
from django.conf import settings as dj_settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, Branch, Address
from finance.models import PaymentMethod, PurchaseInvoice, SupplierPayment
from finance.split_payment import store_credit_method
from finance.supplier_payment_methods import pick_method_for_legacy, legacy_code_for
from inventory.models import Supplier
from users.models import User

URL = '/api/finance/supplier-payments/'


def _m(name, is_cash=False, is_agel=False, is_store_credit=False, is_deleted=False):
    return SimpleNamespace(name=name, is_cash=is_cash, is_agel=is_agel,
                           is_store_credit=is_store_credit, is_deleted=is_deleted)


class LegacyMappingFunctionTests(TestCase):
    """The pure mapping rule shared by the data migration and the old-client fallback."""

    def test_cash_maps_to_first_usable_cash_method(self):
        gone = _m('Old cash', is_cash=True, is_deleted=True)
        cash1, cash2 = _m('Cash', is_cash=True), _m('Safe', is_cash=True)
        self.assertIs(pick_method_for_legacy([gone, cash1, cash2], 'CASH'), cash1)

    def test_bank_and_card_match_by_name_any_case_or_arabic(self):
        insta, bank, card = _m('InstaPay'), _m('NBE BANK transfer'), _m('Credit Card')
        self.assertIs(pick_method_for_legacy([insta, bank, card], 'BANK'), bank)
        self.assertIs(pick_method_for_legacy([insta, bank, card], 'card'), card)
        ar_bank = _m('تحويل بنكي')
        self.assertIs(pick_method_for_legacy([insta, ar_bank], 'BANK'), ar_bank)

    def test_other_or_no_match_is_none(self):
        methods = [_m('Cash', is_cash=True), _m('InstaPay')]
        self.assertIsNone(pick_method_for_legacy(methods, 'OTHER'))
        self.assertIsNone(pick_method_for_legacy(methods, 'BANK'))
        self.assertIsNone(pick_method_for_legacy([], 'CASH'))

    def test_never_picks_agel_or_store_credit(self):
        agel_card = _m('Card on Ajel', is_agel=True)
        wallet = _m('Store credit card', is_store_credit=True)
        self.assertIsNone(pick_method_for_legacy([agel_card, wallet], 'CARD'))

    def test_legacy_code_for_a_chosen_method(self):
        self.assertEqual(legacy_code_for(_m('Cash', is_cash=True)), 'CASH')
        self.assertEqual(legacy_code_for(_m('Bank Misr')), 'BANK')
        self.assertEqual(legacy_code_for(_m('Credit Card')), 'CARD')
        self.assertEqual(legacy_code_for(_m('InstaPay')), 'OTHER')


class _Base(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_spm', password='x', role='OWNER')
        self.store = Store.objects.create(name='SPM', store_code='121', owner=self.owner)   # seeds the 5 default methods
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        self.manager = User.objects.create_user(username='mgr_spm', password='x', role='MANAGER', store=self.store)
        self.supplier = Supplier.objects.create(store=self.store, name='Proline Co', code_prefix='530',
                                                prefix_locked=True)
        pm = PaymentMethod.all_objects.filter(store=self.store)
        self.cash = pm.get(name='Cash')
        self.card = pm.get(name='Credit Card')
        self.insta = pm.get(name='InstaPay')
        self.agel = pm.get(is_agel=True)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.manager).access_token))

    def _pay(self, **body):
        data = {'supplier': str(self.supplier.id), 'amount': '100.00'}
        data.update(body)
        return self.client.post(URL, data, format='json')


class SupplierPaymentMethodApiTests(_Base):

    def test_pay_with_own_method_returns_its_name(self):
        r = self._pay(payment_method=str(self.insta.id))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['payment_method'], self.insta.id)
        self.assertEqual(r.data['payment_method_name'], 'InstaPay')
        p = SupplierPayment.objects.get(pk=r.data['id'])
        self.assertEqual(p.payment_method, self.insta)
        self.assertEqual(p.method, 'OTHER')   # old column kept truthful
        r = self._pay(payment_method=str(self.cash.id))
        self.assertEqual(SupplierPayment.objects.get(pk=r.data['id']).method, 'CASH')
        lst = self.client.get(URL, {'supplier': str(self.supplier.id)}).json()
        rows = lst['results'] if isinstance(lst, dict) else lst
        self.assertEqual(sorted(x['payment_method_name'] for x in rows), ['Cash', 'InstaPay'])

    def test_other_shops_method_refused(self):
        o2 = User.objects.create_user(username='own_spm2', password='x', role='OWNER')
        s2 = Store.objects.create(name='SPM2', store_code='122', owner=o2)
        theirs = PaymentMethod.all_objects.get(store=s2, name='Cash')
        r = self._pay(payment_method=str(theirs.id))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('payment_method', r.data)
        self.assertFalse(SupplierPayment.all_objects.filter(store=self.store).exists())

    def test_agel_refused(self):
        r = self._pay(payment_method=str(self.agel.id))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('Ajel', str(r.data['payment_method']))

    def test_store_credit_refused(self):
        wallet = store_credit_method(self.store)
        r = self._pay(payment_method=str(wallet.id))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('Store credit', str(r.data['payment_method']))

    def test_soft_deleted_method_refused(self):
        self.insta.delete()
        r = self._pay(payment_method=str(self.insta.id))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('payment_method', r.data)

    def test_missing_method_refused(self):
        r = self._pay()
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('payment_method', r.data)

    def test_old_client_sending_method_is_mapped(self):
        r = self._pay(method='CASH')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['payment_method'], self.cash.id)
        r = self._pay(method='CARD')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['payment_method_name'], 'Credit Card')

    def test_old_client_unmappable_method_refused(self):
        r = self._pay(method='BANK')   # the default methods have no "bank" one
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('payment_method', r.data)
        r = self._pay(method='OTHER')
        self.assertEqual(r.status_code, 400, r.content)

    def test_edit_note_keeps_method_and_switch_method_validated(self):
        pid = self._pay(payment_method=str(self.cash.id)).data['id']
        r = self.client.patch(f'{URL}{pid}/', {'note': 'receipt #5'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['payment_method_name'], 'Cash')
        r = self.client.patch(f'{URL}{pid}/', {'payment_method': str(self.agel.id)}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        r = self.client.patch(f'{URL}{pid}/', {'payment_method': None}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        r = self.client.patch(f'{URL}{pid}/', {'payment_method': str(self.card.id)}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(SupplierPayment.objects.get(pk=pid).method, 'CARD')

    def test_unmapped_old_payment_shows_old_label(self):
        old = SupplierPayment.objects.create(store=self.store, supplier=self.supplier, amount=Decimal('5'),
                                             method='BANK')
        r = self.client.get(f'{URL}{old.id}/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(r.data['payment_method'])
        self.assertEqual(r.data['payment_method_name'], 'Bank Transfer')
        self.assertEqual(r.data['method'], 'BANK')


class SupplierStatementAndBalanceTests(_Base):

    def setUp(self):
        super().setUp()
        at = lambda m, d: timezone.make_aware(datetime(2026, m, d, 12, 0))   # noqa: E731
        PurchaseInvoice.objects.create(store=self.store, branch=self.branch, supplier=self.supplier,
                                       date=at(1, 5), status=PurchaseInvoice.Status.RECEIVED,
                                       total_amount=Decimal('1000.00'))
        # an old payment from before the switch (fixed code only)
        self.old = SupplierPayment.objects.create(store=self.store, supplier=self.supplier, date=at(1, 10),
                                                  amount=Decimal('300.00'), method='CASH')

    def _balance(self):
        lst = self.client.get('/api/inventory/suppliers/').json()
        rows = lst['results'] if isinstance(lst, dict) else lst
        return Decimal(str(next(s for s in rows if s['id'] == str(self.supplier.id))['balance']))

    def _statement(self):
        r = self.client.get(f'/api/inventory/suppliers/{self.supplier.id}/statement/')
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_statement_shows_method_name(self):
        self._pay(payment_method=str(self.insta.id), date=timezone.make_aware(datetime(2026, 2, 1, 12)).isoformat())
        pays = [l for l in self._statement()['lines'] if l['kind'] == 'payment']
        self.assertEqual([(l['payment_method'], l['payment_method_name']) for l in pays],
                         [(None, 'Cash'), (str(self.insta.id), 'InstaPay')])   # old unmapped → old label

    def test_balance_unchanged_by_switch_and_by_mapping(self):
        before = self._balance()
        self.assertEqual(before, Decimal('700.00'))
        # the data migration fills payment_method on the old row — the balance must not move
        mig = importlib.import_module('finance.migrations.0018_supplier_payment_payment_method')
        mig.map_legacy_methods(live_apps, None)
        self.old.refresh_from_db()
        self.assertEqual(self.old.payment_method, self.cash)
        self.assertEqual(self._balance(), before)
        self.assertEqual(self._statement()['closing_balance'], '700.00')
        # a new payment with the shop's own method lowers it exactly like before
        self.assertEqual(self._pay(payment_method=str(self.card.id), amount='200.00').status_code, 201)
        self.assertEqual(self._balance(), Decimal('500.00'))


class DataMigrationTests(_Base):
    """finance 0018's data step on real rows: CASH → first cash method, CARD → the "card" method, BANK with no
    "bank" method / OTHER → left NULL with the old text, other shops untouched, already-set rows untouched."""

    def test_mapping_rows(self):
        mk = lambda code, store=None, sup=None: SupplierPayment.objects.create(   # noqa: E731
            store=store or self.store, supplier=sup or self.supplier, amount=Decimal('1'), method=code)
        cash, card, bank, other = mk('CASH'), mk('CARD'), mk('BANK'), mk('OTHER')
        preset = mk('CASH')
        preset.payment_method = self.insta
        preset.save(update_fields=['payment_method'])
        # a second shop with a "Bank Misr" method
        o2 = User.objects.create_user(username='own_spm3', password='x', role='OWNER')
        s2 = Store.objects.create(name='SPM3', store_code='123', owner=o2)
        bank_misr = PaymentMethod.all_objects.create(store=s2, name='Bank Misr')
        sup2 = Supplier.objects.create(store=s2, name='S2', code_prefix='540', prefix_locked=True)
        bank2 = mk('BANK', store=s2, sup=sup2)

        mig = importlib.import_module('finance.migrations.0018_supplier_payment_payment_method')
        mig.map_legacy_methods(live_apps, None)

        got = {p.pk: p.payment_method_id for p in SupplierPayment.all_objects.all()}
        self.assertEqual(got[cash.pk], self.cash.id)
        self.assertEqual(got[card.pk], self.card.id)
        self.assertIsNone(got[bank.pk])
        self.assertIsNone(got[other.pk])
        self.assertEqual(got[preset.pk], self.insta.id)
        self.assertEqual(got[bank2.pk], bank_misr.id)
        bank.refresh_from_db()
        self.assertEqual(bank.method, 'BANK')   # old text kept
