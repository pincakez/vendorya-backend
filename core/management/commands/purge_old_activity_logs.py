"""Audit-log retention purge.

ActivityLog grows forever. This command deletes entries older than a retention
window so the table stays manageable. Run it MANUALLY (matches Vendorya's
manual-over-automation preference) — there is no scheduler wired.

    manage.py purge_old_activity_logs              # default: keep 2 years
    manage.py purge_old_activity_logs --years 1
    manage.py purge_old_activity_logs --days 90
    manage.py purge_old_activity_logs --dry-run    # report only, delete nothing

Deletes in batches to avoid locking a huge table in one transaction.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import ActivityLog


class Command(BaseCommand):
    help = "Delete ActivityLog rows older than the retention window (default 2 years)."

    def add_arguments(self, parser):
        parser.add_argument('--years', type=int, default=None,
                            help="Retention in years (default 2 when neither --years nor --days given).")
        parser.add_argument('--days', type=int, default=None,
                            help="Retention in days (overrides --years if both given).")
        parser.add_argument('--batch-size', type=int, default=5000,
                            help="Rows deleted per batch (default 5000).")
        parser.add_argument('--dry-run', action='store_true',
                            help="Report how many rows would be deleted, delete nothing.")

    def handle(self, *args, **options):
        days = options['days']
        years = options['years']
        if days is not None:
            if days < 1:
                raise CommandError("--days must be >= 1")
            cutoff = timezone.now() - timedelta(days=days)
            window = f"{days} day(s)"
        else:
            yrs = years if years is not None else 2
            if yrs < 1:
                raise CommandError("--years must be >= 1")
            cutoff = timezone.now() - timedelta(days=yrs * 365)
            window = f"{yrs} year(s)"

        qs = ActivityLog.objects.filter(timestamp__lt=cutoff)
        total = qs.count()

        if options['dry_run']:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] {total} log(s) older than {window} (before {cutoff:%Y-%m-%d}) would be deleted."
            ))
            return

        if total == 0:
            self.stdout.write(f"No logs older than {window} (before {cutoff:%Y-%m-%d}). Nothing to do.")
            return

        batch = options['batch_size']
        deleted = 0
        while True:
            ids = list(ActivityLog.objects.filter(timestamp__lt=cutoff)
                       .values_list('pk', flat=True)[:batch])
            if not ids:
                break
            ActivityLog.objects.filter(pk__in=ids).delete()
            deleted += len(ids)

        self.stdout.write(self.style.SUCCESS(
            f"Purged {deleted} log(s) older than {window} (before {cutoff:%Y-%m-%d})."
        ))
