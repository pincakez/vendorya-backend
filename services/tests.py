from datetime import date
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store
from notifications.models import Notification
from services.models import Service
from users.models import User


class ServiceEditTests(TestCase):
    """§AUDIT A7 (s156): editing a job's cost or diagnosis built notifications with a field that does not
    exist (`recipient_id`) → the save went through, then the request crashed with a 500."""

    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_s', password='x', role='OWNER')
        self.store = Store.objects.create(name='Svc', store_code='103', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.manager = User.objects.create_user(username='mgr_s', password='x', role='MANAGER', store=self.store)
        self.job = Service.objects.create(store=self.store, client_name='Ali', receive_date=date.today(),
                                          cost=Decimal('100'), created_by=self.owner)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.manager).access_token))

    def test_cost_and_diagnosis_edit_saves_and_notifies(self):
        r = self.client.patch(f'/api/services/{self.job.id}/', {'cost': '250.00', 'diagnosis': 'Bad screen'}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.job.refresh_from_db()
        self.assertEqual(self.job.cost, Decimal('250.00'))
        self.assertTrue(Notification.objects.filter(store=self.store, title__contains='diagnosis & cost').exists())


class ServiceDonePaymentTests(TestCase):
    """Yakot 2026-10-06 picked (a): "Done" asks how the customer paid — like the till. Before, Done made an
    EMPTY invoice with paid=0, so every finished job became customer debt (§AUDIT A8)."""

    def setUp(self):
        from core.models import Branch, Address
        from finance.models import PaymentMethod
        from users.models import Customer
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_d', password='x', role='OWNER')
        self.store = Store.objects.create(name='Svc2', store_code='104', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        Branch.objects.create(store=self.store, name='Main', address=Address.objects.create(store=self.store, street_1='1', city='Cairo'))
        self.cashier = User.objects.create_user(username='csh_d', password='x', role='CASHIER', store=self.store)
        self.walkin = Customer.objects.create(store=self.store, name='Walk-in', phone_number='0', is_walk_in=True)
        self.client_row = Customer.objects.create(store=self.store, name='Ali', phone_number='0102')
        self.cash = PaymentMethod.objects.create(store=self.store, name='Cash', is_cash=True)
        self.agel = PaymentMethod.objects.create(store=self.store, name='Ajel', is_agel=True)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.cashier).access_token))

    def _job(self, cost='300', client=None):
        return Service.objects.create(store=self.store, client=client, client_name='Ali', receive_date=date.today(),
                                      cost=Decimal(cost), created_by=self.owner)

    def _done(self, job, method=None):
        body = {'method': str(method.id)} if method else {}
        return self.api.post(f'/api/services/{job.id}/done/', body, format='json')

    def test_cash_done_is_paid(self):
        job = self._job()
        r = self._done(job, self.cash)
        self.assertEqual(r.status_code, 200, r.content[:300])
        job.refresh_from_db()
        self.assertEqual(job.invoice.paid_amount, Decimal('300'))
        self.assertEqual(job.invoice.payments.get().created_by, self.cashier)

    def test_done_without_method_refused(self):
        job = self._job()
        self.assertEqual(self._done(job).status_code, 400)
        job.refresh_from_db()
        self.assertNotEqual(job.status, Service.Status.DONE)

    def test_ajel_done_becomes_named_customers_debt(self):
        from finance.models import customer_outstanding
        job = self._job(client=self.client_row)
        self.assertEqual(self._done(job, self.agel).status_code, 200)
        self.assertEqual(customer_outstanding(self.client_row), Decimal('300'))

    def test_ajel_done_needs_named_customer(self):
        self.assertEqual(self._done(self._job(), self.agel).status_code, 400)

    def test_free_job_needs_no_method(self):
        self.assertEqual(self._done(self._job(cost='0')).status_code, 200)
