"""The owner level's shared gate (TREASURY.md § 12.0) — every `/api/treasury/owner/...` view inherits it.

Order: authentication + role (role_map, OWNER for everything; sudo refused like everywhere) → a shop → both switches on
(else 403 `owner_level_off`) → the login IS the owner (else 403 `not_owner`). Data is then scoped by `self.owner`, NEVER by
`request.user.store` alone: business and personal rows have no shop. Another owner's row = 404, never revealed.
"""
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated

from users.permissions import RoleScopedPermission

from . import ledger
from .ledger import TreasuryError
from .models import MoneyBox, OwnerPart


class OwnerMixin:
    permission_classes = [IsAuthenticated, RoleScopedPermission]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        store = getattr(request.user, 'store', None)
        if store is None:
            raise PermissionDenied({'code': 'no_store', 'detail': 'User has no store assigned.'})
        if not ledger.owner_level_on(store):
            raise PermissionDenied({'code': 'owner_level_off', 'detail': 'The owner level is turned off for this shop.'})
        owner = ledger.owner_for_user(request.user)
        if owner is None:
            raise PermissionDenied({'code': 'not_owner', 'detail': 'Only the owner can open the owner level.'})
        self.owner = owner

    def handle_exception(self, exc):
        if isinstance(exc, TreasuryError):
            from .views import treasury_error_response
            return treasury_error_response(exc)
        return super().handle_exception(exc)

    # ── scoped lookups (404 for anything not the owner's) ──
    def owner_part(self, part_id):
        return get_object_or_404(ledger.owner_parts(self.owner), pk=part_id)

    def owner_box(self, box_id):
        return get_object_or_404(ledger.owner_boxes(self.owner).select_related('part'), pk=box_id)
