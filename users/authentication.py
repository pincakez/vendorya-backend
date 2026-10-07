from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken


class VendoryaJWTAuthentication(JWTAuthentication):
    """
    Standard JWT auth that also arms the tenant-scoped managers for the request.

    §PRIVACY-SUDO (s163, Yakot 2026-10-08): a platform super-admin is ALWAYS storeless. The old
    X-Store-ID "Enter store" swap (sudo acting as any shop's owner) was a privacy breach and is gone.

    Rejects "pre-auth" tokens (the short-lived token issued mid-login to let a
    user enrol in 2FA): those are only valid on the 2FA enrolment endpoints.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, token = result
        if token.get('pre_auth'):
            raise InvalidToken('Pre-auth token cannot be used for general API access.')

        if user.is_authenticated and getattr(user, 'is_superadmin', False):
            user.store = None   # belt and braces — the model + migration already keep it NULL

        # Arm the tenant-scoped managers for the rest of this request. For a normal user this is
        # their store; for sudo it's None, which `TenantScopedQuerySet.current_tenant` turns into
        # NO rows (sudo reads shops only through the admin API's explicit `all_objects`).
        from core.tenancy import set_current_store
        set_current_store(getattr(user, 'store', None))

        return (user, token)


class PreAuthJWTAuthentication(JWTAuthentication):
    """Accepts both normal access tokens and short-lived pre-auth tokens.

    Used only by the 2FA enrolment endpoints so a user who is required to set up
    2FA (but hasn't yet) can enrol before they hold a full access token.
    """
    pass
