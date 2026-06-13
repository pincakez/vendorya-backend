"""Scope catalog for the public API.

A scope = ``<resource-group>:<access-level>`` where access-level is ``read``
(safe/GET) or ``write`` (create/update/delete). ``write`` implies ``read``.

A key carries a flat list of scope strings (e.g. ``["inventory:read",
"sales:write"]``). ViewSets declare which resource group they belong to via
``api_scope_resource``; ``HasAPIScope`` maps the HTTP method to read/write and
checks membership. This catalog also feeds the future visual scope builder on
``/admin/api-control``.
"""

READ = 'read'
WRITE = 'write'

# resource-group -> human label. One row per protectable area of the API.
RESOURCE_GROUPS = {
    'inventory':  'Products, variants, stock, categories',
    'sales':      'Sales invoices, POS transactions',
    'purchasing': 'Purchase invoices, suppliers',
    'people':     'Customers',
    'finance':    'Payments, refunds, expenses, shifts',
    'reports':    'Reports & analytics (read-only data)',
}

ACCESS_LEVELS = (READ, WRITE)


def all_scopes():
    """Every grantable scope string, e.g. ['inventory:read', 'inventory:write', ...]."""
    out = []
    for group in RESOURCE_GROUPS:
        for level in ACCESS_LEVELS:
            out.append(f'{group}:{level}')
    return out


def is_valid_scope(scope):
    try:
        group, level = scope.split(':', 1)
    except ValueError:
        return False
    return group in RESOURCE_GROUPS and level in ACCESS_LEVELS


def normalize_scopes(scopes):
    """Drop unknown/malformed scopes; de-dup; keep stable order."""
    seen, out = set(), []
    for s in scopes or []:
        s = (s or '').strip().lower()
        if s and is_valid_scope(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def required_scope(resource, method):
    """The scope a request needs: write for unsafe methods, read for safe ones."""
    level = READ if method in ('GET', 'HEAD', 'OPTIONS') else WRITE
    return f'{resource}:{level}'


def grants(held_scopes, needed):
    """Does the key's scope list satisfy `needed`? write implies read."""
    if needed in held_scopes:
        return True
    # write implies read for the same resource group
    group, level = needed.split(':', 1)
    if level == READ and f'{group}:{WRITE}' in held_scopes:
        return True
    return False
