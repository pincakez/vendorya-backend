from rest_framework.permissions import BasePermission

from .models import User


# Higher rank = more authority. Super-admin sits above OWNER.
ROLE_RANK = {
    User.Role.CASHIER: 1,
    User.Role.MANAGER: 2,
    User.Role.ADMIN:   3,
    User.Role.OWNER:   4,
}
SUPERADMIN_RANK = 99


def _user_rank(user):
    if not user or not user.is_authenticated:
        return 0
    if getattr(user, 'is_superadmin', False):
        return SUPERADMIN_RANK
    return ROLE_RANK.get(user.role, 0)


def _required_rank(role):
    if role is None:
        return 0
    return ROLE_RANK.get(role, 0)


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


class _MinRolePermission(BasePermission):
    """Base class — subclasses set `min_role`."""
    min_role = User.Role.CASHIER
    message = "Insufficient role for this action."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(user, 'is_superadmin', False):
            return True
        if not user.is_active:
            return False
        return _user_rank(user) >= _required_rank(self.min_role)


class IsCashierOrAbove(_MinRolePermission):
    min_role = User.Role.CASHIER


class IsManagerOrAbove(_MinRolePermission):
    min_role = User.Role.MANAGER
    message = "Manager role or higher required."


class IsAdminOrAbove(_MinRolePermission):
    min_role = User.Role.ADMIN
    message = "Admin role or higher required."


class IsOwner(_MinRolePermission):
    min_role = User.Role.OWNER
    message = "Owner role required."


class RoleScopedPermission(BasePermission):
    """
    Per-action role enforcement driven by `view.role_map`.

    Usage on a ViewSet:
        permission_classes = [IsAuthenticated, RoleScopedPermission]
        role_map = {
            'list':           'CASHIER',
            'retrieve':       'CASHIER',
            'create':         'MANAGER',
            'update':         'MANAGER',
            'partial_update': 'MANAGER',
            'destroy':        'ADMIN',
            # custom @action endpoints by their action name, e.g.:
            'void':           'MANAGER',
        }

    Falls back to `view.default_min_role` (defaults to OWNER — fail-closed) if
    an action is not listed.  Super-admins always pass.  Inactive users fail.
    """
    message = "Insufficient role for this action."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(user, 'is_superadmin', False):
            return True
        if not user.is_active:
            return False

        role_map = getattr(view, 'role_map', None) or {}
        default = getattr(view, 'default_min_role', User.Role.OWNER)
        required = role_map.get(view.action, default)
        return _user_rank(user) >= _required_rank(required)
