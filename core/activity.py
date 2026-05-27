"""Helpers for writing ActivityLog entries from API actions."""
from .models import ActivityLog


def _client_ip(request):
    if not request:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def log_activity(*, request, action, op_type, details=None, store=None):
    """
    Create an ActivityLog row for the active request's store + user.

    Pass `store` explicitly only when the request's user has no store attached
    (e.g. a super-admin acting via X-Store-ID where you've already resolved
    the target store some other way). In the common case the helper picks
    `request.user.store`, which our VendoryaJWTAuthentication already swaps
    for the X-Store-ID target when a super-admin is acting on a store.
    """
    if request is None:
        return None

    user = getattr(request, 'user', None)
    if not user or not getattr(user, 'is_authenticated', False):
        return None

    resolved_store = store or getattr(user, 'store', None)
    if not resolved_store:
        # No store context — silently drop (e.g. super-admin in General Admin mode).
        return None

    op_value = op_type
    if hasattr(op_type, 'value'):
        op_value = op_type.value

    return ActivityLog.objects.create(
        store=resolved_store,
        user=user,
        operation_type=op_value,
        action=action,
        details=details or {},
        ip_address=_client_ip(request),
    )
