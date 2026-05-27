from rest_framework_simplejwt.authentication import JWTAuthentication


class VendoryaJWTAuthentication(JWTAuthentication):
    """
    Standard JWT auth, but for super-admin requests it honors the
    X-Store-ID header by swapping the authenticated user's .store
    attribute to the requested store. Regular users are unaffected.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, token = result
        if user.is_authenticated and getattr(user, 'is_superadmin', False):
            store_id = request.META.get('HTTP_X_STORE_ID')
            if store_id:
                from core.models import Store
                try:
                    store = Store.objects.get(id=store_id, is_active=True, is_deleted=False)
                    user.store = store
                except (Store.DoesNotExist, ValueError):
                    user.store = None

        return (user, token)
