"""Test runner that keeps `manage.py test` away from the live search server (s166).

The product signals index every saved Product into Typesense. Under tests that meant every
test product landed in the real dev collection (`products_dev`) and was never cleaned up.
Here the client is switched to "unconfigured" for the whole run, so every indexing helper
is a no-op and every search falls back to pg_trgm. Tests that need Typesense mock it
(see search/tests.py) — they never touch a live server.
"""
from django.test.runner import DiscoverRunner


class NoLiveSearchRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        from search import client as ts
        self._ts_key = ts.API_KEY
        ts.API_KEY = ''
        ts._clients.clear()

    def teardown_test_environment(self, **kwargs):
        from search import client as ts
        ts.API_KEY = self._ts_key
        super().teardown_test_environment(**kwargs)
