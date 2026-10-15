"""s171: the extra customer fields (2nd phone, address, national ID, tax number, guarantor) — saved by staff,
national ID + guarantor hidden from cashiers by default (core/field_visibility.py), searchable by 2nd phone."""
from django.conf import settings as dj_settings
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, StoreSettings
from users.models import User, Customer

URL = '/api/auth/customers/'
PRIVATE = ['national_id', 'guarantor_name', 'guarantor_national_id', 'guarantor_phone', 'guarantor_phone_2',
           'guarantor_work', 'guarantor_address']


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')
    return c


class CustomerFieldsTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_cf', password='x', role='OWNER')
        self.store = Store.objects.create(name='CF', store_code='151', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        StoreSettings.objects.get_or_create(store=self.store)
        self.manager = User.objects.create_user(username='mgr_cf', password='x', role='MANAGER', store=self.store)
        self.cashier = User.objects.create_user(username='csh_cf', password='x', role='CASHIER', store=self.store)
        self.mgr = _client(self.manager)
        self.body = {'name': 'Mona', 'phone_number': '01000000001', 'phone_2': '01200000002', 'address': '5 Nile St',
                     'national_id': '29001011234567', 'tax_number': '123-456-789',
                     'guarantor_name': 'Hassan', 'guarantor_national_id': '28001011234567',
                     'guarantor_phone': '01100000003', 'guarantor_phone_2': '', 'guarantor_work': 'Teacher',
                     'guarantor_address': '7 Nile St'}

    def test_manager_saves_and_reads_every_field(self):
        r = self.mgr.post(URL, self.body, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        c = Customer.objects.get(pk=r.data['id'])
        self.assertEqual((c.national_id, c.guarantor_name, c.tax_number), ('29001011234567', 'Hassan', '123-456-789'))
        got = self.mgr.get(f"{URL}{r.data['id']}/").data
        for k, v in self.body.items():
            self.assertEqual(got[k], v, k)

    def test_cashier_never_receives_personal_fields(self):
        cid = self.mgr.post(URL, self.body, format='json').data['id']
        csh = _client(self.cashier)
        one = csh.get(f'{URL}{cid}/').data
        for k in PRIVATE:
            self.assertNotIn(k, one)
        self.assertEqual((one['phone_2'], one['address']), ('01200000002', '5 Nile St'))
        lst = csh.get(URL).json()
        rows = lst['results'] if isinstance(lst, dict) else lst
        self.assertTrue(rows and all('national_id' not in row for row in rows))

    def test_search_by_second_phone_not_by_national_id(self):
        self.mgr.post(URL, self.body, format='json')
        def hits(q):
            d = self.mgr.get(URL, {'search': q}).json()
            return [x['name'] for x in (d['results'] if isinstance(d, dict) else d)]
        self.assertIn('Mona', hits('01200000002'))
        self.assertNotIn('Mona', hits('29001011234567'))
