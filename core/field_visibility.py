"""Layer 1 — server-enforced field visibility (per-store, by role).

This is the security boundary: hidden fields are removed from the API response
entirely, so a hidden column never reaches the client (presets/UI can only ever
hide *within* what a user is already permitted to see — never reveal).
"""

# Built-in defaults applied when a store hasn't customised `field_visibility`.
# Sensitive margin columns are hidden from cashiers by default.
DEFAULT_HIDDEN = {
    'inventory_products': {
        'CASHIER': ['cost_display', 'profit_display'],
    },
    # s171: personal data a cashier doesn't need at the till.
    'people_customers': {
        'CASHIER': ['national_id', 'guarantor_name', 'guarantor_national_id', 'guarantor_phone',
                    'guarantor_phone_2', 'guarantor_work', 'guarantor_address'],
    },
}

# Roles that always see everything (they manage visibility; can't be locked out).
_FULL_ACCESS_ROLES = {'OWNER', 'ADMIN'}


def hidden_fields_for(user, table_id):
    """Return the set of field names `user` must NOT receive for `table_id`."""
    if user is None or getattr(user, 'is_superadmin', False):
        return set()
    role = getattr(user, 'role', None)
    if role in _FULL_ACCESS_ROLES:
        return set()

    cfg = None
    store = getattr(user, 'store', None)
    settings = getattr(store, 'settings', None) if store else None
    if settings and settings.field_visibility:
        cfg = settings.field_visibility.get(table_id)
    if cfg is None:
        cfg = DEFAULT_HIDDEN.get(table_id, {})

    return set(cfg.get(role, []))


class FieldVisibilityMixin:
    """Drop role-hidden fields from a serializer's output. Set `table_id`."""
    table_id = None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        if request is not None and self.table_id:
            for field in hidden_fields_for(request.user, self.table_id):
                data.pop(field, None)
        return data


def hides_cost(user):
    """True when `user` must receive NO cost figure anywhere (s167, Yakot: "hide cost from cashiers").
    Follows the products-table rule for `cost_display`, so ONE switch (the shop's field_visibility,
    default = hidden for CASHIER) covers the list, the product page, the variants API and storage."""
    return 'cost_display' in hidden_fields_for(user, 'inventory_products')


def hides_supplier(user):
    """Same idea for the supplier: hidden on the product page only when the shop hides the
    products-table supplier column for this role (not hidden by default)."""
    return 'supplier_name' in hidden_fields_for(user, 'inventory_products')


def request_user(serializer):
    request = serializer.context.get('request')
    return getattr(request, 'user', None) if request is not None else None
