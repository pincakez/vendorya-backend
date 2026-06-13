"""Scope enforcement for API-key traffic.

`HasAPIScope` only constrains requests authenticated with an APIKey; JWT
(first-party app) requests pass straight through, so adding this permission to a
ViewSet never affects the web app. A ViewSet opts in by declaring the resource
group it belongs to:

    class ProductViewSet(...):
        permission_classes = [IsAuthenticated, RoleScopedPermission, HasAPIScope]
        api_scope_resource = 'inventory'

The request must then carry `inventory:read` (safe methods) or `inventory:write`
(unsafe). This stacks with RoleScopedPermission — a key can never exceed its
owner's role AND must hold the scope.
"""
from rest_framework.permissions import BasePermission

from .models import APIKey
from .scopes import required_scope, grants


class HasAPIScope(BasePermission):
    message = 'This API key lacks the required scope for this resource.'

    def has_permission(self, request, view):
        api_key = request.auth
        if not isinstance(api_key, APIKey):
            return True  # not API-key auth (JWT/session) — scopes don't apply

        resource = getattr(view, 'api_scope_resource', None)
        if resource is None:
            # ViewSet not annotated for API exposure → API keys can't reach it.
            self.message = 'This endpoint is not exposed to API keys.'
            return False

        needed = required_scope(resource, request.method)
        if grants(api_key.scopes or [], needed):
            return True
        self.message = f"API key missing scope '{needed}'."
        return False
