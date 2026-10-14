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


class PosHidesGhostsInsideSearchTests(TestCase):
    """s166: the till asked Typesense for the top 20, THEN dropped ghosted products — so hidden products
    crowded visible ones out. Now the index carries `hide_from_pos` and the POS query filters on it."""

    def setUp(self):
        self.owner = User.objects.create_user(username='own2', password='pw', role='OWNER')
        self.store = Store.objects.create(name='S2', store_code='102', owner=self.owner)
        self.owner.store = self.store
        self.owner.save()
        StoreSettings.objects.get_or_create(store=self.store)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.shown = Product.objects.create(store=self.store, name='Cable shown')
        self.ghost = Product.objects.create(store=self.store, name='Cable ghost', hide_from_pos=True)

    def test_document_carries_the_ghost_flag(self):
        self.assertIs(indexing.build_document(self.ghost)['hide_from_pos'], True)
        self.assertIs(indexing.build_document(self.shown)['hide_from_pos'], False)

    def test_bulk_ghost_sends_the_new_flag(self):
        fake, docs = _fake_ts()
        with mock.patch.object(indexing, 'ts', fake), self.captureOnCommitCallbacks(execute=True):
            self.api.post('/api/inventory/products/bulk_ghost/',
                          {'ids': [str(self.shown.id)], 'hide': True}, format='json')
        sent = [d for call in docs.import_.call_args_list for d in call.args[0]]
        self.assertEqual([(d['id'], d['hide_from_pos']) for d in sent], [(str(self.shown.id), True)])

    def test_pos_query_filters_ghosts_in_typesense(self):
        from search import client as ts
        fake_client = mock.MagicMock()
        search = fake_client.collections.__getitem__.return_value.documents.search
        search.return_value = {'hits': []}
        with mock.patch.object(ts, 'get_client', return_value=fake_client):
            ts.search_ids('s1', 'cab', store_history=True, pos=True)
            ts.search_ids('s1', 'cab')
        pos_filter, plain_filter = (c.args[0]['filter_by'] for c in search.call_args_list)
        self.assertIn('hide_from_pos:false', pos_filter)
        self.assertNotIn('hide_from_pos', plain_filter)

    def test_autocomplete_pos_mode_asks_for_the_filter(self):
        from search import client as ts
        with mock.patch.object(ts, 'is_configured', return_value=True), \
             mock.patch.object(ts, 'search_ids', return_value=[str(self.shown.id)]) as sid:
            r = self.api.get('/api/inventory/products/autocomplete/', {'q': 'cab', 'pos': 1})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(sid.call_args.kwargs['pos'])
        self.assertEqual([p['id'] for p in r.data['results']], [str(self.shown.id)])

    def test_old_collection_without_the_field_falls_back_to_the_database(self):
        """Before `ts_reindex` the live collection has no hide_from_pos field → Typesense errors →
        the pg_trgm path answers, and it already excludes ghosts."""
        from search import client as ts
        with mock.patch.object(ts, 'is_configured', return_value=True), \
             mock.patch.object(ts, 'search_ids', side_effect=Exception('no filter field hide_from_pos')):
            r = self.api.get('/api/inventory/products/autocomplete/', {'q': 'cable', 'pos': 1})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([p['id'] for p in r.data['results']], [str(self.shown.id)])


class TestRunNeverTouchesLiveSearchTests(TestCase):
    """s166: the suite used to write every test product into the live dev collection."""

    def test_typesense_is_switched_off_under_tests(self):
        from search import client as ts
        self.assertFalse(ts.is_configured())

    def test_saving_a_product_makes_no_search_call(self):
        from search import client as ts
        owner = User.objects.create_user(username='own3', password='pw', role='OWNER')
        store = Store.objects.create(name='S3', store_code='103', owner=owner)
        with mock.patch.object(ts, 'get_client') as gc:
            Product.objects.create(store=store, name='Nothing to index')
        gc.assert_not_called()
