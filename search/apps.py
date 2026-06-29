import threading
from django.apps import AppConfig

_NAV_REINDEX_INTERVAL = 12 * 3600  # 12 hours


def _run_nav_reindex():
    """Reindex the nav collection. Runs in a background daemon thread."""
    try:
        from .client import is_configured, ensure_nav_collection, index_nav_items, NAV_COLLECTION
        from .nav_index import NAV_ITEMS
        if not is_configured():
            return
        ensure_nav_collection()
        index_nav_items(NAV_ITEMS)
    except Exception:
        pass  # never crash the server over a nav reindex


def _schedule_nav_reindex():
    _run_nav_reindex()
    t = threading.Timer(_NAV_REINDEX_INTERVAL, _schedule_nav_reindex)
    t.daemon = True
    t.start()


class SearchConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'search'

    def ready(self):
        from . import signals  # noqa: F401
        # Nav collection: index once on startup, then every 12 h (daemon thread).
        t = threading.Thread(target=_schedule_nav_reindex, daemon=True)
        t.start()
