"""B8 — bulk writes that skip the post_save signal must still keep the search index right.

Typesense is mocked: these tests never need (or touch) a live server.
"""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Store, StoreSettings
from inventory.models import Category, Product
from search import indexing
from users.models import User


def _fake_ts():
    """A stand-in for search.client: configured, with a recording client."""
    fake = mock.MagicMock()
    fake.is_configured.return_value = True
    fake.COLLECTION = 'products_test'
    docs = fake.get_client.return_value.collections.__getitem__.return_value.documents
    return fake, docs


class BulkIndexSyncTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='own', password='pw', role='OWNER')
        self.store = Store.objects.create(name='S', store_code='100', owner=self.owner)
        self.owner.store = self.store
        self.owner.save()
        StoreSettings.objects.get_or_create(store=self.store)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.cat = Category.objects.create(store=self.store, name='Phones')
        self.a = Product.objects.create(store=self.store, name='A', category=self.cat)
        self.b = Product.objects.create(store=self.store, name='B', category=self.cat)
        self.other = Product.objects.create(store=self.store, name='Other')

    def _deleted_ids(self, docs):
        out = set()
        for call in docs.delete.call_args_list:
            flt = call.args[0]['filter_by']           # "id:[x,y]"
            out |= set(flt[len('id:['):-1].split(','))
        return out

    def _upserted_ids(self, docs):
        return {d['id'] for call in docs.import_.call_args_list for d in call.args[0]}

    def test_category_purge_drops_its_products_from_search(self):
        fake, docs = _fake_ts()
        with mock.patch.object(indexing, 'ts', fake), self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(f'/api/inventory/categories/{self.cat.id}/resolve-delete/',
                              {'mode': 'purge', 'reason': 'OTHER'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self._deleted_ids(docs), {str(self.a.id), str(self.b.id)})
        self.assertNotIn(str(self.other.id), self._upserted_ids(docs))

    def test_bulk_ghost_resyncs_exactly_the_chosen_products(self):
        fake, docs = _fake_ts()
        with mock.patch.object(indexing, 'ts', fake), self.captureOnCommitCallbacks(execute=True):
            r = self.api.post('/api/inventory/products/bulk_ghost/',
                              {'ids': [str(self.a.id), str(self.b.id)], 'hide': True}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self._upserted_ids(docs), {str(self.a.id), str(self.b.id)})
        docs.delete.assert_not_called()

    def test_sync_never_raises_when_typesense_is_down(self):
        fake, docs = _fake_ts()
        docs.import_.side_effect = ConnectionError('down')
        with mock.patch.object(indexing, 'ts', fake):
            indexing.sync_products([self.a.id])          # must not raise

    def test_sync_does_nothing_when_unconfigured(self):
        fake, docs = _fake_ts()
        fake.is_configured.return_value = False
        with mock.patch.object(indexing, 'ts', fake):
            indexing.sync_products([self.a.id])
        fake.get_client.assert_not_called()
