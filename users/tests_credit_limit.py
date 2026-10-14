from decimal import Decimal

from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from finance.tests import _TillBase
from users.models import Customer, User

CURL = '/api/auth/customers/'


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class CustomerCreditLimitRoleTests(_TillBase):
    """Yakot 2026-10-14: a customer's credit limit (how much they may owe) is set by a manager and up only —
    same rule as the opening balance (s167). A cashier still adds and edits customers; sending the unchanged
    limit, or leaving it out, is fine and never wipes it."""

    def setUp(self):
        super().setUp()
        self.manager = User.objects.create_user(username='mgr_cl', password='x', role='MANAGER', store=self.store)
        self.mgr = _client(self.manager)   # self.client = the cashier

    def _limit(self):
        self.customer.refresh_from_db()
        return self.customer.credit_limit

    def test_manager_sets_and_changes_it(self):
        r = self.mgr.post(CURL, {'name': 'Trusted', 'credit_limit': '5000'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Customer.objects.get(pk=r.data['id']).credit_limit, Decimal('5000'))
        r = self.mgr.patch(f'{CURL}{self.customer.id}/', {'credit_limit': '1500'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self._limit(), Decimal('1500'))
        r = self.mgr.patch(f'{CURL}{self.customer.id}/', {'credit_limit': None}, format='json')   # back to shop default
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(self._limit())

    def test_cashier_cannot_set_or_change_it(self):
        r = self.client.post(CURL, {'name': 'Sneaky', 'credit_limit': '99999'}, format='json')
        self.assertEqual(r.status_code, 403, r.content)
        self.assertFalse(Customer.objects.filter(store=self.store, name='Sneaky').exists())
        Customer.objects.filter(pk=self.customer.pk).update(credit_limit=Decimal('1000'))
        r = self.client.patch(f'{CURL}{self.customer.id}/', {'credit_limit': '50000'}, format='json')
        self.assertEqual(r.status_code, 403, r.content)
        r = self.client.patch(f'{CURL}{self.customer.id}/', {'credit_limit': None}, format='json')   # nor remove it
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(self._limit(), Decimal('1000'))

    def test_cashier_edits_the_name_and_the_limit_stays(self):
        Customer.objects.filter(pk=self.customer.pk).update(credit_limit=Decimal('1000'))
        r = self.client.patch(f'{CURL}{self.customer.id}/', {'name': 'Buyer Renamed', 'notes': 'vip'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.name, self.customer.credit_limit), ('Buyer Renamed', Decimal('1000')))
        # the unchanged value coming along with a full form is fine too
        r = self.client.patch(f'{CURL}{self.customer.id}/', {'name': 'Buyer', 'credit_limit': '1000.00'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self._limit(), Decimal('1000'))
        # …and a cashier still adds a customer with no limit
        r = self.client.post(CURL, {'name': 'New at the till'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsNone(Customer.objects.get(pk=r.data['id']).credit_limit)
