from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken


class VendoryaJWTAuthentication(JWTAuthentication):
    """
    Standard JWT auth, but for super-admin requests it honors the
    X-Store-ID header by swapping the authenticated user's .store
    attribute to the requested store. Regular users are unaffected.

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
            store_id = request.META.get('HTTP_X_STORE_ID')
            if store_id:
                from core.models import Store
                try:
                    store = Store.objects.get(id=store_id, is_active=True, is_deleted=False)
                    user.store = store
                except (Store.DoesNotExist, ValueError):
                    user.store = None

        # Arm the tenant-scoped managers for the rest of this request now that
        # the real user (and any sudo acting-store) is resolved. For a normal
        # user this is their store; for un-acting sudo it's None (= all rows).
        from core.tenancy import set_current_store
        set_current_store(getattr(user, 'store', None))

        return (user, token)


class PreAuthJWTAuthentication(JWTAuthentication):
    """Accepts both normal access tokens and short-lived pre-auth tokens.

    Used only by the 2FA enrolment endpoints so a user who is required to set up
    2FA (but hasn't yet) can enrol before they hold a full access token.
    """
    pass
