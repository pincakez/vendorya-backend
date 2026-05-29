from django.utils import timezone


def send_notification(store, title, body='', priority='INFO', notif_type='GENERAL',
                      user=None, link='', payload=None):
    """
    Create a notification for a store.
    user=None  → store-wide (visible to every member of the store).
    user=<obj> → addressed to one specific user only.
    """
    from .models import Notification

    n = Notification.objects.create(
        store=store,
        user=user,
        priority=priority,
        type=notif_type,
        title=title,
        body=body,
        link=link,
        payload=payload or {},
    )
    _purge_after_create(store, priority)
    return n


def _purge_after_create(store, priority):
    """
    ADMIN notes: keep newest 100 per store, hard-delete the rest.
    System (INFO/WARNING/ALERT): hard-delete anything older than 90 days.
    """
    from .models import Notification

    if priority == Notification.Priority.ADMIN:
        keep_ids = list(
            Notification.objects.filter(store=store, priority=Notification.Priority.ADMIN)
            .order_by('-created_at')
            .values_list('id', flat=True)[:100]
        )
        Notification.objects.filter(
            store=store, priority=Notification.Priority.ADMIN,
        ).exclude(id__in=keep_ids).delete()
    else:
        cutoff = timezone.now() - timezone.timedelta(days=90)
        Notification.objects.filter(
            store=store,
            priority__in=[
                Notification.Priority.INFO,
                Notification.Priority.WARNING,
                Notification.Priority.ALERT,
            ],
            created_at__lt=cutoff,
        ).delete()
