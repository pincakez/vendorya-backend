"""Memory Base de-duplication (superfix §2.6).

The Memory Base reference pool accumulates entries from product creation, purchase
items, drafts and CSV import — so duplicate-named entries creep in over time. This
command collapses them per store: for each duplicated (case-insensitive) name it
keeps the richest entry (most attributes, oldest wins a tie) and soft-deletes the
rest (recoverable via admin Trash).

Yakot explicitly opted into running this on a nightly schedule (an exception to
Vendorya's usual manual-over-automation rule). Wire it to cron / a systemd timer,
or run it by hand — the same logic also backs the "Remove duplicates" button on
the Memory Base page.

    manage.py dedup_memory_base            # de-dup every store
    manage.py dedup_memory_base --dry-run  # report counts, delete nothing
"""

from django.core.management.base import BaseCommand

from core.models import Store
from inventory.models import Product
from inventory.product_service import dedup_memory_base_for_store


class Command(BaseCommand):
    help = "Collapse duplicate-named Memory Base entries per store (keep the richest)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help="Report how many would be removed, delete nothing.")

    def handle(self, *args, **options):
        dry = options['dry_run']
        total = 0
        for store in Store.objects.all():
            if dry:
                # Count duplicates without deleting: group by lower(name).
                from collections import Counter
                names = (Product.objects
                         .filter(store=store, source=Product.Source.MEMORY_BASE)
                         .values_list('name', flat=True))
                counts = Counter((n or '').strip().lower() for n in names if (n or '').strip())
                removable = sum(c - 1 for c in counts.values() if c > 1)
            else:
                removable = dedup_memory_base_for_store(store)
            if removable:
                self.stdout.write(f"{store.name}: {removable} duplicate(s) "
                                  f"{'would be ' if dry else ''}removed")
            total += removable
        verb = 'would remove' if dry else 'removed'
        self.stdout.write(self.style.SUCCESS(f"Memory Base dedup: {verb} {total} entry(ies) across all stores."))
