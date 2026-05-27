"""Per-request middleware for tenant scoping."""
from .tenancy import set_current_request, clear_current_request


class TenantContextMiddleware:
    """Stashes the request (and its resolved store) in a thread-local for the
    duration of the request.  Pairs with `core.tenancy.TenantScopedManager`
    to auto-filter querysets by the active tenant.

    Must run AFTER DRF authentication, but since DRF resolves the user
    lazily, this middleware just needs to wrap each request — the lookup
    happens when ORM code asks for the current tenant.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        set_current_request(request)
        try:
            return self.get_response(request)
        finally:
            clear_current_request()
