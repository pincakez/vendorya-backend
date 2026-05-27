from rest_framework.permissions import BasePermission


class IsSuperAdmin(BasePermission):
    """Allows access only to Vendorya platform super-admins."""
    message = "Super-admin privileges required."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(user, 'is_superadmin', False)
        )
