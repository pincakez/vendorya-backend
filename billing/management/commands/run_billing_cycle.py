"""Nightly billing cycle.

Advances the subscription lifecycle based on the platform `BillingSettings`:

  1. Expired trials      → PAST_DUE   (trial_ends_at has passed)
  2. Overdue invoices    → PAST_DUE   (an ISSUED invoice is past its due_at)
  3. Delinquent past-due → suspend    (delinquent longer than grace_days → store.is_active = False)

Idempotent — safe to run repeatedly. Stamps `BillingSettings.last_run_at`.

Schedule it once a day (systemd timer / cron):
    manage.py run_billing_cycle
The sudo "Run cycle now" button calls it with force=True.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from billing.models import BillingSettings, Subscription, BillingInvoice
from core.models import ActivityLog


class Command(BaseCommand):
    help = "Advance the subscription lifecycle (expire trials, flag past-due, suspend delinquent stores)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help="Run even when nightly_job_enabled is off (used by the 'Run now' button).",
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        settings_obj = BillingSettings.load()
        force   = options['force']
        dry_run = options['dry_run']

        if not settings_obj.nightly_job_enabled and not force:
            self.stdout.write("Nightly job disabled (nightly_job_enabled=False). Skipping.")
            return

        today = timezone.localdate()
        S = Subscription.Status

        expired_trials = []
        flagged_overdue = []
        suspended = []

        # 1. Expired trials → PAST_DUE
        trial_qs = Subscription.objects.filter(
            status=S.TRIAL, trial_ends_at__isnull=False, trial_ends_at__lt=today,
        )
        for sub in trial_qs:
            expired_trials.append(sub.store.name)
            if not dry_run:
                sub.status = S.PAST_DUE
                sub.save(update_fields=['status', 'updated_at'])

        # 2. Subscriptions with an overdue ISSUED invoice → PAST_DUE (if not already)
        overdue_invoice_subs = (BillingInvoice.objects
                                .filter(status=BillingInvoice.Status.ISSUED,
                                        due_at__isnull=False, due_at__lt=today)
                                .values_list('subscription_id', flat=True)
                                .distinct())
        flag_qs = Subscription.objects.filter(
            id__in=list(overdue_invoice_subs),
        ).exclude(status__in=[S.PAST_DUE, S.CANCELLED])
        for sub in flag_qs.select_related('store'):
            flagged_overdue.append(sub.store.name)
            if not dry_run:
                sub.status = S.PAST_DUE
                sub.save(update_fields=['status', 'updated_at'])

        # 3. PAST_DUE longer than grace_days → suspend the store
        grace = timedelta(days=settings_obj.grace_days)
        pastdue_qs = (Subscription.objects
                      .filter(status=S.PAST_DUE, store__is_active=True, store__is_deleted=False)
                      .select_related('store'))
        for sub in pastdue_qs:
            delinquent_since = self._delinquent_since(sub, today)
            if delinquent_since is None:
                continue
            if today > delinquent_since + grace:
                suspended.append(sub.store.name)
                if not dry_run:
                    self._suspend(sub)

        # Stamp last run (skip on dry-run)
        if not dry_run:
            settings_obj.last_run_at = timezone.now()
            settings_obj.save(update_fields=['last_run_at', 'updated_at'])

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}Billing cycle done — "
            f"{len(expired_trials)} trial(s) expired, "
            f"{len(flagged_overdue)} flagged past-due, "
            f"{len(suspended)} store(s) suspended."
        ))
        for name in suspended:
            self.stdout.write(f"  suspended: {name}")

    def _delinquent_since(self, sub, today):
        """Earliest date this subscription became delinquent.

        Uses the oldest unpaid ISSUED invoice due_at, falling back to
        trial_ends_at when the trial lapsed without any invoice. Returns None
        when there's nothing to anchor a grace window to.
        """
        candidates = []
        oldest_due = (sub.invoices
                      .filter(status=BillingInvoice.Status.ISSUED,
                              due_at__isnull=False, due_at__lt=today)
                      .order_by('due_at')
                      .values_list('due_at', flat=True)
                      .first())
        if oldest_due:
            candidates.append(oldest_due)
        if sub.trial_ends_at and sub.trial_ends_at < today:
            candidates.append(sub.trial_ends_at)
        return min(candidates) if candidates else None

    @transaction.atomic
    def _suspend(self, sub):
        store = sub.store
        store.is_active = False
        store.save(update_fields=['is_active', 'updated_at'])
        ActivityLog.objects.create(
            store=store,
            user=None,
            operation_type=ActivityLog.OperationType.OTHER,
            action="Store suspended — payment past due beyond grace period",
            details={'reason': 'billing_past_due', 'subscription_status': sub.status},
        )
