from decimal import Decimal

from django.utils import timezone


# ── s167 (PROLINE-PARITY Upgrade 12): notifications in the READER's language ──
# A notification stores a stable message key + params in payload['i18n'];
# the bell / Inbox render `notif.<key>` from the reader's own locale file
# (en.json / ar.json). title/body are still stored, in English — they are the
# fallback for old rows (no i18n block), sudo history and any unknown key.
# Shape: {'title': 'low_stock.title', 'body': 'low_stock.body' | absent,
#         'params': {name: str | int | {'k': word_key}}}
# A param {'k': 'walk_in'} is a translatable WORD (notif.words.walk_in).
def word(key):
    """A param the frontend translates too (e.g. 'Walk-in')."""
    return {'k': key}


def _param(v):
    if v is None:
        return ''
    if isinstance(v, (dict, int, str)) and not isinstance(v, bool):
        return v
    return str(v)   # Decimal money keeps its 2 places ("150.00")


def qty_str(v):
    """A stock quantity without trailing zeros: Decimal('3.000') → '3', '2.500' → '2.5'."""
    d = Decimal(str(v))
    s = format(d.normalize(), 'f')
    return s


def msg(title_key, body_key=None, **params):
    """Build the payload['i18n'] block. body_key=None → the stored body is shown as-is
    (free text such as a technician's diagnosis)."""
    data = {'title': title_key, 'params': {k: _param(v) for k, v in params.items()}}
    if body_key:
        data['body'] = body_key
    return data


def send_notification(store, title, body='', priority='INFO', notif_type='GENERAL',
                      user=None, link='', payload=None, i18n=None):
    """
    Create a notification for a store.
    user=None  → store-wide (visible to every member of the store).
    user=<obj> → addressed to one specific user only.
    i18n=msg(...) → s167: key + params so each reader sees it in their own language.
    """
    from .models import Notification

    payload = dict(payload or {})
    if i18n:
        payload['i18n'] = i18n
    n = Notification.objects.create(
        store=store,
        user=user,
        priority=priority,
        type=notif_type,
        title=title,
        body=body,
        link=link,
        payload=payload,
    )
    _purge_after_create(store, priority)
    return n


def _purge_after_create(store, priority):
    """
    ADMIN notes: keep newest 100 per store, hard-delete the rest.
    System (INFO/WARNING/ALERT): hard-delete anything older than 90 days.
    """
    from .models import Notification

    # Infra keyed by an explicit `store` (may run inside a sudo-acting request) → all_objects.
    if priority == Notification.Priority.ADMIN:
        keep_ids = list(
            Notification.all_objects.filter(store=store, priority=Notification.Priority.ADMIN)
            .order_by('-created_at')
            .values_list('id', flat=True)[:100]
        )
        Notification.all_objects.filter(
            store=store, priority=Notification.Priority.ADMIN,
        ).exclude(id__in=keep_ids).delete()
    else:
        cutoff = timezone.now() - timezone.timedelta(days=90)
        Notification.all_objects.filter(
            store=store,
            priority__in=[
                Notification.Priority.INFO,
                Notification.Priority.WARNING,
                Notification.Priority.ALERT,
            ],
            created_at__lt=cutoff,
        ).delete()
