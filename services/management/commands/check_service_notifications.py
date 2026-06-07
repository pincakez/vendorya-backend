"""
Scan open services with notify_bell=True and fire an in-app notification
when the ETA is within the store's service_notify_hours window.

Intended to run every 15 minutes via cron/systemd:
  */15 * * * * /path/to/venv/bin/python manage.py check_service_notifications

Each service fires at most once (notified=True prevents re-firing).
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Fire ETA notifications for services whose bell is on and ETA is approaching.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would fire without creating notifications.')

    def handle(self, *args, **options):
        from services.models import Service
        from notifications.dispatcher import send_notification
        from notifications.models import Notification

        dry_run = options['dry_run']
        now = timezone.now()
        fired = 0
        skipped = 0

        candidates = (
            Service.objects
            .filter(
                notify_bell=True,
                notified=False,
                no_eta=False,
                eta_datetime__isnull=False,
                status=Service.Status.OPEN,
                is_deleted=False,
            )
            .select_related('store__settings', 'client')
        )

        for svc in candidates:
            settings = getattr(svc.store, 'settings', None)
            notify_hours = getattr(settings, 'service_notify_hours', 1)

            if notify_hours == 0:
                skipped += 1
                continue

            delta_seconds = (svc.eta_datetime - now).total_seconds()
            threshold_seconds = notify_hours * 3600

            # Fire if ETA is within the window (and hasn't passed yet)
            if 0 <= delta_seconds <= threshold_seconds:
                client_label = (
                    svc.client.name if svc.client_id
                    else svc.client_name or 'Walk-in'
                )
                hours_left = int(delta_seconds // 3600)
                minutes_left = int((delta_seconds % 3600) // 60)
                time_str = f"{hours_left}h {minutes_left}m" if hours_left else f"{minutes_left}m"

                if dry_run:
                    self.stdout.write(
                        f"[DRY RUN] Would notify: {svc.serial_number} — {client_label} "
                        f"(ETA in {time_str})"
                    )
                else:
                    send_notification(
                        store=svc.store,
                        title=f"Service ETA: {svc.serial_number}",
                        body=(
                            f"{client_label} — {svc.service_type or 'Service'} "
                            f"is due in {time_str}."
                        ),
                        priority=Notification.Priority.WARNING,
                        notif_type=Notification.Type.GENERAL,
                        link='/services',
                        payload={'service_id': str(svc.id)},
                    )
                    svc.notified = True
                    svc.save(update_fields=['notified', 'updated_at'])

                fired += 1

        verb = 'Would fire' if dry_run else 'Fired'
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {fired} notification(s). Skipped {skipped} (notify disabled)."
            )
        )
