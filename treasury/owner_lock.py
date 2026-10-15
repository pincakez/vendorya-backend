"""s174 (Yakot): "My money" behind the owner's own authenticator.

Opening the My money tree shows the app's lock screen; the owner types the 6-digit Google Authenticator code (or one of his
backup codes) and the owner level stays open `StoreSettings.owner_unlock_minutes`. The SERVER keeps the lock: every
`/api/treasury/owner/...` view answers 403 `owner_locked` until then (OwnerMixin), so a direct link or an API call is refused
too. Never the shop's lock-screen PIN — the staff know that one.

One open window per LOGIN TOKEN (the access token's jti) in the shared cache — another device of the same owner stays locked.
"""
from django.core.cache import cache
from django.utils import timezone

MAX_TRIES = 5          # wrong codes per login before a pause …
PAUSE_SECONDS = 300    # … of 5 minutes (django-otp also throttles the device itself)


def enabled(store):
    s = getattr(store, 'settings', None)
    return bool(s and s.owner_lock_enabled and s.treasury_enabled and s.treasury_owner_level)


def _token_id(request):
    try:
        return (request.auth.get('jti') if request.auth is not None else None) or 'none'
    except AttributeError:
        return 'none'


def _key(request):
    return f'owner-unlock:{request.user.pk}:{_token_id(request)}'


def _fail_key(request):
    return f'owner-unlock-fail:{request.user.pk}'


def seconds_left(request):
    until = cache.get(_key(request))
    if not until:
        return 0
    return max(0, int(until - timezone.now().timestamp()))


def is_open(request):
    return seconds_left(request) > 0


def open_with_code(request, code):
    """→ 'ok' | 'wrong' | 'paused'. A right code (or backup code) opens the owner level for the shop's minutes."""
    from users.twofa import verify_token
    fails = cache.get(_fail_key(request), 0)
    if fails >= MAX_TRIES:
        return 'paused'
    if verify_token(request.user, str(code or '').strip().replace(' ', '')) is None:
        cache.set(_fail_key(request), fails + 1, PAUSE_SECONDS)
        return 'wrong'
    cache.delete(_fail_key(request))
    minutes = request.user.store.settings.owner_unlock_minutes or 15
    cache.set(_key(request), timezone.now().timestamp() + minutes * 60, minutes * 60)
    return 'ok'


def close(request):
    cache.delete(_key(request))
