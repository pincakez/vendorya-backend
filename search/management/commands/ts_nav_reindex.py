"""Reindex the Typesense nav collection from the static nav_index.py catalogue.

Run manually:   manage.py ts_nav_reindex
Called by:      NavSearchView POST ?action=reindex  (admin-only)
Scheduled by:   cron via core.views every 12 hours
"""
from django.core.management.base import BaseCommand

from search.client import ensure_nav_collection, index_nav_items, is_configured, NAV_COLLECTION
from search.nav_index import NAV_ITEMS


class Command(BaseCommand):
    help = 'Rebuild the Typesense nav collection from the static nav_index.'

    def handle(self, *args, **options):
        if not is_configured():
            self.stderr.write('TYPESENSE_API_KEY not set — skipping.')
            return
        self.stdout.write(f'Rebuilding {NAV_COLLECTION} ({len(NAV_ITEMS)} items)…')
        ensure_nav_collection()
        index_nav_items(NAV_ITEMS)
        self.stdout.write(self.style.SUCCESS(f'Done — {len(NAV_ITEMS)} nav items indexed.'))
