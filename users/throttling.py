from rest_framework.throttling import ScopedRateThrottle

from core.security import get_client_ip


class LoginRateThrottle(ScopedRateThrottle):
    """Per-IP rate limit for the login endpoint (anti credential-stuffing).

    Keyed on the real client IP (CF-Connecting-IP behind the tunnel) so a single
    source can't hammer /api/auth/token/ regardless of which usernames it tries.
    Complements django-axes (per-account lockout) which acts on a different axis.
    """
    scope = 'login'

    def get_cache_key(self, request, view):
        return self.cache_format % {
            'scope': self.scope,
            'ident': get_client_ip(request),
        }
