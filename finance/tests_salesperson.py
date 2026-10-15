"""s171: who SOLD a sale (and each line) — defaults to the signed-in person, can be another person of the same shop
(never another shop's), the name is frozen, and the staff report counts it apart from the money taken."""
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store
from finance.models import SalesInvoice
from finance.tests import _TillBase
from users.models import User


class SalespersonTests(_TillBase):
    def setUp(self):
        super().setUp()
        self.seller = User.objects.create_user(username='sell_t', password='x', role='CASHIER', store=self.store,
                                               first_name='Mona', last_name='Seller')
        self.manager = User.objects.create_user(username='mgr_sp', password='x', role='MANAGER', store=self.store)

    def _cart(self, **extra):
        body = {'branch': str(self.branch.id), 'customer': str(self.customer.id), 'status': 'DRAFT',
                'date': timezone.now().isoformat(),
                'items': [{'variant': str(self.variant.id), 'quantity': '1', 'unit_price': '100',
                           **extra.pop('line', {})}], **extra}
        r = self.client.post('/api/finance/invoices/', body, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def test_defaults_to_the_signed_in_person_on_the_sale_and_its_lines(self):
        d = self._cart()
        self.assertEqual((str(d['salesperson']), d['salesperson_name']), (str(self.cashier.id), 'csh_t'))
        self.assertEqual(d['items'][0]['salesperson_name'], 'csh_t')

    def test_another_person_of_the_shop_and_per_line(self):
        d = self._cart(salesperson=str(self.seller.id), line={'salesperson': str(self.manager.id)})
        self.assertEqual(d['salesperson_name'], 'Mona Seller')
        self.assertEqual(d['items'][0]['salesperson_name'], 'mgr_sp')

    def test_other_shops_person_refused(self):
        o = User.objects.create_user(username='own_x', password='x', role='OWNER')
        other = Store.objects.create(name='X', store_code='109', owner=o)
        stranger = User.objects.create_user(username='str_x', password='x', role='CASHIER', store=other)
        r = self.client.post('/api/finance/invoices/', {
            'branch': str(self.branch.id), 'customer': str(self.customer.id), 'status': 'DRAFT',
            'date': timezone.now().isoformat(), 'salesperson': str(stranger.id), 'items': []}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('salesperson', r.data)

    def test_name_survives_the_login_going_away(self):
        d = self._cart(salesperson=str(self.seller.id))
        self.seller.delete()
        inv = SalesInvoice.objects.get(pk=d['id'])
        self.assertEqual((inv.salesperson_id, inv.salesperson_name), (None, 'Mona Seller'))

    def test_staff_report_counts_what_each_person_sold(self):
        d = self._cart(salesperson=str(self.seller.id))
        r = self.client.post(self.URL.format(d['id']), {'method': str(self.cash.id)}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        mc = APIClient()
        mc.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.manager).access_token))
        today = timezone.localdate().isoformat()
        rep = mc.get('/api/reports/cashier-performance/', {'date_from': today, 'date_to': today}).json()
        rows = {row['name']: row for row in rep['rows']}
        self.assertEqual((rows['Mona Seller']['sold_count'], rows['Mona Seller']['sold_value']), (1, '100.00'))
        self.assertEqual(rows['Mona Seller']['sales_count'], 0)      # the cashier took the money, not her
        self.assertEqual(rows['csh_t']['sales_count'], 1)
