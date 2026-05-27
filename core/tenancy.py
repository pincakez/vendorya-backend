"""
Tenant isolation backstop.

Pattern:
- Middleware (`core.middleware.TenantContextMiddleware`) stashes the active
  request and resolved store in a thread-local on every authenticated request.
- ViewSets / ad-hoc code can call `get_current_store()` to retrieve the active
  tenant without threading `request` through every helper.
- Tenant-scoped models can opt in to `TenantScopedManager` (exposed as
  `.tenant_objects`) which auto-filters by the active store.  Existing
  `.objects` is untouched so legacy queries still work and admin/management
  commands are unaffected.

This is *defense in depth*. ViewSets must still filter by store explicitly
today — the manager + middleware combo is the safety net so that new code
written against `Model.tenant_objects` cannot accidentally cross tenants.
"""
import threading

from django.db import models


_state = threading.local()


# ---------- thread-local helpers ----------

def set_current_request(request):
    _state.request = request
    _state.store = getattr(getattr(request, 'user', None), 'store', None)


def clear_current_request():
    if hasattr(_state, 'request'):
        del _state.request
    if hasattr(_state, 'store'):
        del _state.store


def get_current_request():
    return getattr(_state, 'request', None)


def get_current_store():
    return getattr(_state, 'store', None)


def is_superadmin_context():
    req = get_current_request()
    if not req:
        return False
    user = getattr(req, 'user', None)
    return bool(user and getattr(user, 'is_superadmin', False))


# ---------- manager + queryset ----------

class TenantScopedQuerySet(models.QuerySet):
    """A queryset that, by default, restricts to the active tenant.

    Falls back to all rows when no request is in flight (management commands,
    migrations, signals fired from a shell, etc.) so non-request code paths
    are not silently broken.
    """

    #: dotted-path lookup from the model to its Store FK
    #: subclasses set this when the relation isn't named `store` (e.g. for
    #: indirect tenant scoping like `branch__store`).
    tenant_lookup = 'store'

    def for_tenant(self, store):
        if store is None:
            return self
        return self.filter(**{self.tenant_lookup: store})

    def current_tenant(self):
        store = get_current_store()
        if store is None:
            # Outside a request context (management command, signal) — return
            # the full queryset.  Super-admin without X-Store-ID also lands
            # here.  Caller is responsible for narrowing further if needed.
            return self
        return self.for_tenant(store)


class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    """Manager whose default queryset is already scoped to the active tenant.

    Use as an *additional* manager on tenant-scoped models:

        class Product(...):
            objects = models.Manager()           # legacy: no auto-scoping
            tenant_objects = TenantScopedManager()

    `tenant_objects.all()` then returns only the active tenant's rows in a
    request context.  Outside of a request (management command, migration)
    it returns everything — same as `.objects`.
    """

    tenant_lookup = 'store'

    def __init__(self, tenant_lookup=None):
        super().__init__()
        if tenant_lookup is not None:
            self.tenant_lookup = tenant_lookup

    def get_queryset(self):
        qs = TenantScopedQuerySet(self.model, using=self._db)
        qs.tenant_lookup = self.tenant_lookup
        return qs.current_tenant()
