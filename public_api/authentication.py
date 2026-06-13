"""API-key authentication, sitting alongside JWT.

A request authenticates with a key via either header:
    Authorization: Api-Key vdy_xxxx_xxxxxxxx...
    X-API-Key: vdy_xxxx_xxxxxxxx...

On success the request authenticates AS the key's owner (so role-based
permissions still apply), bound to the key's store, and `request.auth` is the
APIKey instance (carrying `.scopes` for HasAPIScope). The tenant thread-local
is armed to the key's store so the secure-by-default managers scope correctly —
identical to the JWT path.
"""
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from .models import APIKey

KEYWORD = 'api-key'   # Authorization: Api-Key <key>


class APIKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        raw_key = self._extract_key(request)
        if not raw_key:
            return None  # no API key present — let JWT (or anon) handle it

        api_key = APIKey.resolve(raw_key)
        if api_key is None:
            raise AuthenticationFailed('Invalid, expired, or revoked API key.')

        user = api_key.created_by
        if user is None or not user.is_active:
            raise AuthenticationFailed('API key owner is missing or inactive.')

        # Bind the request to the key's store and arm the tenant managers, exactly
        # like the JWT path does, so isolation holds for API-key traffic too.
        user.store = api_key.store
        from core.tenancy import set_current_store
        set_current_store(api_key.store)

        api_key.touch()
        return (user, api_key)

    def authenticate_header(self, request):
        # Drives the WWW-Authenticate header on 401s.
        return 'Api-Key'

    @staticmethod
    def _extract_key(request):
        # 1) Authorization: Api-Key <key>
        auth = get_authorization_header(request).split()
        if len(auth) == 2 and auth[0].lower() == KEYWORD.encode():
            return auth[1].decode()
        # 2) X-API-Key: <key>
        x = request.META.get('HTTP_X_API_KEY')
        if x:
            return x.strip()
        return None
