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
