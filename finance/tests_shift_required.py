from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Address, Branch
from finance.models import Payment, SalesInvoice, WorkShift
from finance.tests import _TillBase
from inventory.models import StockLevel
from users.models import User


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class RequireShiftToSellTests(_TillBase):
    """Yakot 2026-10-14: the shop setting "Require an open shift to sell" (off by default). When on, the till's
    checkout — and a manager posting a draft by edit — is refused unless the seller has their OWN open shift in
    the sale's branch. A refused sale stays a DRAFT: no number, no stock out, no payment."""

    def _stock(self):
        return StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity

    def _turn_on(self):
        self.settings.require_shift_to_sell = True
        self.settings.save(update_fields=['require_shift_to_sell'])

    def _open_shift(self, user=None, branch=None):
        return WorkShift.objects.create(store=self.store, branch=branch or self.branch, user=user or self.cashier)

    def test_off_by_default_sells_as_today(self):
        self.assertFalse(self.settings.require_shift_to_sell)
        inv = self._draft()
        r = self._checkout(inv, self.cash)
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SalesInvoice.Status.POSTED)

    def test_on_without_a_shift_is_refused_and_nothing_moves(self):
        self._turn_on()
        inv = self._draft()
        before = self._stock()
        r = self._checkout(inv, self.cash)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['code'], 'shift_required')
        self.assertIsInstance(r.data['detail'], str)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SalesInvoice.Status.DRAFT)
        self.assertEqual(inv.paid_amount, Decimal('0'))
        self.assertEqual(Payment.objects.filter(invoice=inv).count(), 0)
        self.assertEqual(self._stock(), before)

    def test_on_with_an_open_shift_sells(self):
        self._turn_on()
        self._open_shift()
        inv = self._draft()
        before = self._stock()
        r = self._checkout(inv, self.cash)
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SalesInvoice.Status.POSTED)
        self.assertEqual(inv.paid_amount, inv.grand_total)
        self.assertEqual(self._stock(), before - 1)

    def test_a_closed_shift_or_someone_elses_or_another_branch_does_not_count(self):
        self._turn_on()
        WorkShift.objects.create(store=self.store, branch=self.branch, user=self.cashier,
                                 status=WorkShift.Status.CLOSED)
        other_cashier = User.objects.create_user(username='csh_2', password='x', role='CASHIER', store=self.store)
        self._open_shift(user=other_cashier)
        addr = Address.objects.create(store=self.store, street_1='2', city='Giza')
        branch2 = Branch.objects.create(store=self.store, name='Second', address=addr)
        self._open_shift(branch=branch2)
        inv = self._draft()
        r = self._checkout(inv, self.cash)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['code'], 'shift_required')

    def test_manager_posting_a_draft_by_edit_follows_the_same_rule(self):
        self._turn_on()
        manager = User.objects.create_user(username='mgr_s', password='x', role='MANAGER', store=self.store)
        mgr = _client(manager)
        inv = self._draft()
        url = f'/api/finance/invoices/{inv.id}/'
        r = mgr.patch(url, {'status': 'POSTED'}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['code'], 'shift_required')
        inv.refresh_from_db()
        self.assertEqual(inv.status, SalesInvoice.Status.DRAFT)
        self._open_shift(user=manager)
        r = mgr.patch(url, {'status': 'POSTED'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SalesInvoice.Status.POSTED)

    def test_only_the_owner_turns_it_on(self):
        r = self.client.patch('/api/core/settings/', {'require_shift_to_sell': True}, format='json')
        self.assertEqual(r.status_code, 403, r.content)
        r = _client(self.owner).patch('/api/core/settings/', {'require_shift_to_sell': True}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.settings.refresh_from_db()
        self.assertTrue(self.settings.require_shift_to_sell)
        self.assertTrue(self.client.get('/api/core/settings/').data['require_shift_to_sell'])   # the till reads it


class OneOpenShiftTests(_TillBase):
    """2026-10-14 (surgeon review): a double-click on "Open shift" opened TWO shifts for one person — both counted
    the same sales as cash in → a false "cash short". Now the DB allows one OPEN shift per person."""
    SURL = '/api/finance/shifts/'

    def _open(self):
        return self.client.post(self.SURL, {'branch': str(self.branch.id), 'starting_cash': '100'}, format='json')

    def test_second_open_is_refused_cleanly(self):
        self.assertEqual(self._open().status_code, 201)
        r = self._open()
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['code'], 'shift_already_open')
        self.assertEqual(WorkShift.objects.filter(user=self.cashier, status=WorkShift.Status.OPEN).count(), 1)

    def test_the_race_is_stopped_by_the_database(self):
        # A row the view's pre-check can't see (another shop) stands in for the twin request that won the race.
        from core.models import Store
        other = Store.objects.create(name='T2', store_code='102', owner=self.owner)
        a2 = Address.objects.create(store=other, street_1='9', city='Alex')
        b2 = Branch.objects.create(store=other, name='B2', address=a2)
        WorkShift.all_objects.create(store=other, branch=b2, user=self.cashier)
        r = self._open()
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['code'], 'shift_already_open')
        self.assertEqual(WorkShift.all_objects.filter(user=self.cashier, status=WorkShift.Status.OPEN).count(), 1)

    def test_closed_shifts_do_not_count(self):
        WorkShift.objects.create(store=self.store, branch=self.branch, user=self.cashier, status=WorkShift.Status.CLOSED)
        WorkShift.objects.create(store=self.store, branch=self.branch, user=self.cashier, status=WorkShift.Status.CLOSED)
        self.assertEqual(self._open().status_code, 201)


class SaleIsBornADraftTests(_TillBase):
    """2026-10-14 (surgeon review): POST /invoices/ with status=POSTED made a completed sale that skipped checkout
    (open shift, credit selling off, stock) — even for a cashier. A new sale is always a draft now."""
    IURL = '/api/finance/invoices/'

    def _body(self, status_):
        return {'branch': str(self.branch.id), 'customer': str(self.customer.id), 'status': status_, 'discount': 0,
                'date': timezone.now().isoformat(),
                'items': [{'variant': str(self.variant.id), 'unit': None, 'quantity': 1, 'unit_price': '100',
                           'discount_amount': 0}]}

    def test_creating_a_posted_sale_is_refused(self):
        before = self._stock()
        manager = User.objects.create_user(username='mgr_d', password='x', role='MANAGER', store=self.store)
        for c in (self.client, _client(manager)):
            r = c.post(self.IURL, self._body('POSTED'), format='json')
            self.assertEqual(r.status_code, 400, r.content)
            self.assertIn('status', r.data)
        self.assertFalse(SalesInvoice.objects.filter(store=self.store).exists())
        self.assertEqual(self._stock(), before)

    def test_a_draft_still_works_and_stays_a_draft(self):
        r = self.client.post(self.IURL, self._body('DRAFT'), format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['status'], 'DRAFT')
        body = self._body('DRAFT')
        body.pop('status')
        r = self.client.post(self.IURL, body, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(SalesInvoice.objects.get(pk=r.data['id']).status, SalesInvoice.Status.DRAFT)

    def _stock(self):
        return StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity
