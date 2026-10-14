from decimal import Decimal

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
