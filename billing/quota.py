"""Plan-quota enforcement.

Per-plan limits live on `SubscriptionPlan` (max_users / max_branches /
max_products / max_invoices_per_month). The platform-wide `BillingSettings.quota_mode`
decides how they behave:

    BLOCK → `enforce_quota` raises ValidationError when the store is at/over limit
    WARN  → always allowed; over-limit surfaces via `quota_status` for the UI to flag
    OFF   → quotas ignored entirely

A NULL limit on the plan means "unlimited" for that resource.
"""

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import BillingSettings, Subscription


# resource key -> SubscriptionPlan field
_LIMIT_FIELD = {
    'users':    'max_users',
    'branches': 'max_branches',
    'products': 'max_products',
    'invoices': 'max_invoices_per_month',
}

_LABEL = {
    'users':    'users',
    'branches': 'branches',
    'products': 'products',
    'invoices': 'invoices this month',
}


def _limit_for(store, resource):
    """Return the numeric limit for `resource`, or None if unlimited / no plan."""
    sub = (Subscription.objects
           .select_related('plan')
           .filter(store=store)
           .first())
    if not sub:
        return None
    return getattr(sub.plan, _LIMIT_FIELD[resource], None)


def _usage_for(store, resource):
    """Current count of `resource` for `store`."""
    # Imported lazily to avoid app-loading circular imports.
    if resource == 'users':
        from users.models import User
        return User.objects.filter(store=store, is_active=True).count()
    if resource == 'branches':
        from core.models import Branch
        return Branch.objects.filter(store=store, is_deleted=False).count()
    if resource == 'products':
        from inventory.models import Product
        return Product.objects.filter(store=store, is_deleted=False).count()
    if resource == 'invoices':
        from finance.models import SalesInvoice
        now = timezone.localtime()
        return SalesInvoice.objects.filter(
            store=store, is_deleted=False,
            created_at__year=now.year, created_at__month=now.month,
        ).count()
    return 0


def enforce_quota(store, resource):
    """Call right before creating `resource` for `store`.

    In BLOCK mode, raises rest_framework ValidationError when the store has
    already reached its limit. In WARN/OFF mode this is a no-op (the action is
    always allowed). Returns the limit that was checked (or None).
    """
    if store is None:
        return None
    mode = BillingSettings.load().quota_mode
    if mode == BillingSettings.QuotaMode.OFF:
        return None

    limit = _limit_for(store, resource)
    if limit is None:
        return None  # unlimited / no plan

    if mode == BillingSettings.QuotaMode.BLOCK and _usage_for(store, resource) >= limit:
        raise ValidationError({
            'detail': (
                f"Plan limit reached: your plan allows {limit} {_LABEL[resource]}. "
                f"Upgrade your plan to add more."
            ),
            'quota': {'resource': resource, 'limit': limit},
        })
    return limit


def quota_status(store):
    """Per-resource usage snapshot for a store — drives the UI 'flag' in WARN mode.

    Returns {resource: {used, limit, over}} where limit is None for unlimited.
    """
    out = {}
    for resource in _LIMIT_FIELD:
        limit = _limit_for(store, resource)
        used  = _usage_for(store, resource)
        out[resource] = {
            'used':  used,
            'limit': limit,
            'over':  bool(limit is not None and used >= limit),
        }
    return out
