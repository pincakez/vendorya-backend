"""Rebuild the Typesense product index from the database.

Idempotent + safe to re-run: drops and recreates the collection, then bulk-imports
every active product (STORE + MEMORY_BASE) across all stores. Run on prod after
`migrate` during a savegame.
"""
from django.core.management.base import BaseCommand

from search import client as ts
from search.indexing import reindex_all


class Command(BaseCommand):
    help = "Rebuild the Typesense product index from the database (idempotent)."

    def handle(self, *args, **options):
        if not ts.is_configured():
            self.stderr.write(self.style.WARNING(
                "TYPESENSE_API_KEY not set — nothing to do (autocomplete will use pg_trgm)."))
            return
        self.stdout.write(
            f"Reindexing into '{ts.COLLECTION}' @ {ts.HOST}:{ts.PORT} …")
        total, failed = reindex_all(stdout=lambda m: self.stdout.write(m))
        style = self.style.SUCCESS if failed == 0 else self.style.WARNING
        self.stdout.write(style(f"Done. {total} documents indexed ({failed} failed)."))
