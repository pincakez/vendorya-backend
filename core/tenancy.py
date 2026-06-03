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
    """Stash the request at the very start of the cycle.

    NOTE: we do NOT resolve the store here — middleware runs *before* DRF
    authenticates, so `request.user` is still anonymous at this point. The
    store is pushed separately by the authentication layer (see
    `set_current_store`) once the real user is known. Resolving it here was a
    latent no-op bug that made the whole tenant manager scope to None (= all
    rows) forever.
    """
    _state.request = request


def set_current_store(store):
    """Record the active tenant for the rest of this request.

    Called from `VendoryaJWTAuthentication` right after the user (and, for
    sudo, the X-Store-ID acting-store) is resolved. This is what actually
    arms the tenant-scoped managers.
    """
    _state.store = store


def clear_current_request():
    if hasattr(_state, 'request'):
        del _state.request
    if hasattr(_state, 'store'):
        del _state.store


def get_current_request():
    return getattr(_state, 'request', None)


def get_current_store():
    """The active tenant, or None outside a request / for un-acting sudo.

    Falls back to resolving from the live request's user if the auth layer
    hasn't pushed it explicitly (defensive — e.g. session-auth code paths).
    """
    store = getattr(_state, 'store', None)
    if store is not None:
        return store
    req = get_current_request()
    if req is None:
        return None
    return getattr(getattr(req, 'user', None), 'store', None)


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

    def _clone(self):
        # Carry `tenant_lookup` across every chained operation (.filter(),
        # .all(), .order_by(), ...). Django's _clone() doesn't copy custom
        # attributes, so without this a single .filter() before current_tenant()
        # would silently reset the lookup to the class default.
        clone = super()._clone()
        clone.tenant_lookup = self.tenant_lookup
        return clone

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

    Intended as the secure-by-default `objects` on tenant-scoped models that
    do NOT carry soft-delete (plain TimestampedModel):

        class WorkShift(TimestampedModel):
            objects     = TenantScopedManager()   # auto-scoped
            all_objects = models.Manager()         # escape hatch (cross-tenant)

    In a request context `objects.all()` returns only the active tenant's
    rows; outside a request (command/migration) or for un-acting sudo it
    returns everything — same as a plain manager. Cross-tenant code (sudo
    admin API, isolation audit) must go through `all_objects`.
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


class TenantSoftDeleteManager(TenantScopedManager):
    """`objects` for tenant models that ALSO soft-delete (SoftDeleteModel).

    Combines the two default behaviours that `.objects` already implied on
    those models — hide `is_deleted=True` rows — with tenant auto-scoping.
    `all_objects` (the existing GlobalManager on SoftDeleteModel) stays the
    unscoped, includes-deleted escape hatch used by trash + audit + signals.
    """

    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)
