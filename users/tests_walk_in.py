"""s167: GATES' Walk-in customer was found soft-deleted on prod → the till opened sales with no customer.
The Walk-in can't be deleted any more, and migration users 0014 brings one back for every shop missing it."""
import importlib

from django.apps import apps as django_apps
from django.conf import settings as dj_settings
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store
from users.models import Customer, User

restore = importlib.import_module('users.migrations.0014_restore_walk_in_customers').restore_walk_in_customers


class WalkInCustomerTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_wi', password='x', role='OWNER')
        self.store = Store.objects.create(name='WI', store_code='141', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.owner).access_token))

    def _walk_in(self):
        return Customer.all_objects.filter(store=self.store, is_walk_in=True)

    def test_new_shop_has_a_live_walk_in(self):
        self.assertEqual(self._walk_in().filter(is_deleted=False).count(), 1)

    def test_walk_in_cannot_be_deleted(self):
        w = self._walk_in().get()
        r = self.c.delete(f'/api/auth/customers/{w.id}/')
        self.assertEqual(r.status_code, 400, r.content)
        w.refresh_from_db()
        self.assertFalse(w.is_deleted)

    def test_other_customers_still_delete(self):
        x = Customer.objects.create(store=self.store, name='Someone')
        self.assertEqual(self.c.delete(f'/api/auth/customers/{x.id}/').status_code, 204)

    def test_migration_restores_a_deleted_walk_in(self):
        w = self._walk_in().get()
        w.delete()   # soft — what happened to GATES on prod
        restore(django_apps, None)
        w.refresh_from_db()
        self.assertFalse(w.is_deleted)
        self.assertEqual(self._walk_in().count(), 1)   # restored, not duplicated

    def test_migration_creates_one_when_none_exists(self):
        self._walk_in().delete()   # queryset delete = hard delete
        restore(django_apps, None)
        self.assertEqual(self._walk_in().filter(is_deleted=False).count(), 1)

    def test_migration_leaves_a_healthy_shop_alone(self):
        before = list(self._walk_in().values_list('id', flat=True))
        restore(django_apps, None)
        self.assertEqual(list(self._walk_in().values_list('id', flat=True)), before)
