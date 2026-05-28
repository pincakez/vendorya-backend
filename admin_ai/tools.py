"""Built-in AI tools.

C1 ships only two sample tools so the registry has something to introspect
and chat round-trips can be smoke-tested. C3 populates the remaining ~40
read/write tools listed in TODO.md.

When you add a tool here, write it as:

    @tool(name='do_thing', description='...', parameters={...}, write=False)
    def do_thing(context, **kwargs):
        # context.store is the acting store (None if sudo isn't impersonating).
        return {'ok': True}

The function must accept `context` as its first positional arg. Everything
else comes through as keyword args matching the JSON schema.
"""
from .registry import tool


@tool(
    name='get_current_context',
    description='Return the current acting-store context for the Admin AI. '
                'Use this when the user asks "where am I" or "what store am I on".',
    parameters={'type': 'object', 'properties': {}},
    write=False,
)
def get_current_context(context):
    store = context.store
    return {
        'acting_store_id': str(store.id) if store else None,
        'acting_store_name': store.name if store else None,
        'is_platform_view': store is None,
        'user': getattr(context.user, 'username', None),
    }


@tool(
    name='list_stores',
    description='List all stores on the platform. Sudo-only.',
    parameters={
        'type': 'object',
        'properties': {
            'include_inactive': {
                'type': 'boolean',
                'description': 'If True, also return deactivated stores.',
            },
        },
    },
    write=False,
)
def list_stores(context, include_inactive=False):
    from core.models import Store
    qs = Store.objects.filter(is_deleted=False)
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return [{'id': str(s.id), 'name': s.name, 'is_active': s.is_active}
            for s in qs.order_by('name')[:200]]
