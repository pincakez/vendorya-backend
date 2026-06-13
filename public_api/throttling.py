"""Per-key rate limiting.

Only API-key traffic is throttled — JWT/first-party requests return a null
cache key and are never rate-limited (that would throttle the whole web app).
The rate is configured via DEFAULT_THROTTLE_RATES['api_key'] in settings.
"""
from rest_framework.throttling import SimpleRateThrottle

from .models import APIKey


class APIKeyRateThrottle(SimpleRateThrottle):
    scope = 'api_key'

    def get_cache_key(self, request, view):
        api_key = getattr(request, 'auth', None)
        if not isinstance(api_key, APIKey):
            return None  # not API-key auth → don't throttle
        return self.cache_format % {'scope': self.scope, 'ident': str(api_key.pk)}
