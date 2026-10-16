"""Module switches + module control (s176, Yakot 10-15/16).

Every optional module has ONE on/off switch in the shop's own Settings → Modules (a StoreSettings field). Above that,
sudo decides which modules a shop is ALLOWED to switch on:
  * per plan   — SubscriptionPlan.modules: the module keys the plan includes; NULL = every module (all free today, D129);
  * per shop   — Store.module_access: {key: 'allow' | 'block'} exceptions to the plan; no key = follow the plan.

A module that is not allowed can't be switched on (StoreSettingsView refuses `module_not_allowed`), and when sudo takes
a module away, `enforce()` switches it off for the shop — nothing is deleted, the same as the owner turning it off.
Gates elsewhere keep reading the StoreSettings field, so "off" means the same thing whoever turned it off.
"""
from django.db import transaction
from rest_framework.exceptions import ValidationError

# key → the StoreSettings switch (None = no shop switch; only sudo controls it). Order = the admin screen's order.
MODULES = {
    'credit':             'enable_agel_selling',
    'reservations':       'reservations_enabled',
    'payment_dues':       'payment_dues_enabled',
    'cheques':            'cheques_enabled',
    'salesperson_picker': 'salesperson_picker_enabled',
    'multi_unit':         'multi_unit_enabled',
    'expiry':             'expiry_tracking_enabled',
    'weight':             'weight_selling_enabled',
    'serials':            'serials_enabled',
    'transfers':          'transfers_enabled',
    'supplier_returns':   'supplier_returns_enabled',
    'services':           'services_enabled',
    'treasury':           'treasury_enabled',
    'owner_level':        'treasury_owner_level',
    'shop_export':        None,
}
FIELD_TO_KEY = {f: k for k, f in MODULES.items() if f}
# The owner level rides on the Treasury: taking the Treasury away takes the owner level too.
DEPENDS_ON = {'owner_level': 'treasury'}
ACCESS_VALUES = ('allow', 'block')


def plan_modules(store):
    """The keys the shop's plan includes, or None = every module (no subscription / plan.modules NULL)."""
    sub = getattr(store, 'subscription', None) if store is not None else None
    try:
        modules = sub.plan.modules if sub is not None else None
    except Exception:   # a subscription row without a readable plan — treat as "every module"
        modules = None
    return None if modules is None else set(modules)


def allowed(store):
    """{key: bool} — may the shop switch each module on?"""
    included = plan_modules(store)
    overrides = (getattr(store, 'module_access', None) or {}) if store is not None else {}
    out = {}
    for key in MODULES:
        ok = included is None or key in included
        ov = overrides.get(key)
        if ov == 'allow':
            ok = True
        elif ov == 'block':
            ok = False
        out[key] = ok
    for key, base in DEPENDS_ON.items():
        out[key] = out[key] and out[base]
    return out


def is_allowed(store, key):
    return allowed(store).get(key, True)


def clean_module_list(value):
    """Validate SubscriptionPlan.modules input: None (every module) or a list of known keys."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValidationError({'modules': 'A list of module keys, or null for every module.'})
    unknown = [k for k in value if k not in MODULES]
    if unknown:
        raise ValidationError({'modules': f'Unknown module(s): {", ".join(map(str, unknown))}'})
    return sorted(set(value), key=list(MODULES).index)


def clean_access(value):
    """Validate Store.module_access input: {key: 'allow' | 'block'}; 'plan' / None drop the key."""
    if not isinstance(value, dict):
        raise ValidationError({'module_access': 'An object of {module: "allow" | "block" | "plan"}.'})
    out = {}
    for key, v in value.items():
        if key not in MODULES:
            raise ValidationError({'module_access': f'Unknown module: {key}'})
        if v in (None, '', 'plan'):
            continue
        if v not in ACCESS_VALUES:
            raise ValidationError({'module_access': f'{key}: use "allow", "block" or "plan".'})
        out[key] = v
    return out


def refused_switches(store, validated_data):
    """Keys the shop tries to switch ON that it isn't allowed to (StoreSettingsView refuses them)."""
    ok = allowed(store)
    return [FIELD_TO_KEY[f] for f, v in validated_data.items()
            if f in FIELD_TO_KEY and v and not ok[FIELD_TO_KEY[f]]]


@transaction.atomic
def enforce(store):
    """Switch off every module the shop is no longer allowed. Returns the keys switched off.

    Refuses (ValidationError code `shifts_open`) to take the Treasury away while a shift runs on a drawer box —
    the same rule as the owner turning it off (its count would never reach the box). Nothing is deleted."""
    settings_obj = getattr(store, 'settings', None)
    if settings_obj is None:
        return []
    ok = allowed(store)
    off = [key for key, field in MODULES.items() if field and not ok[key] and getattr(settings_obj, field)]
    if not off:
        return []
    if 'treasury' in off:
        from finance.models import WorkShift
        if WorkShift.all_objects.filter(store=store, status=WorkShift.Status.OPEN, ledger=WorkShift.Ledger.BOX).exists():
            raise ValidationError({'code': 'shifts_open',
                                   'detail': 'This shop has an open shift on a Treasury drawer. Its shifts must be '
                                             'closed before the Treasury can be taken away.'})
    for key in off:
        setattr(settings_obj, MODULES[key], False)
    settings_obj.save(update_fields=[MODULES[k] for k in off] + ['updated_at'])
    return off


def enforce_plan(plan):
    """After a plan's module list changes: enforce every shop on that plan. Returns {store name: [keys switched off]}."""
    from billing.models import Subscription
    done = {}
    for sub in Subscription.all_objects.filter(plan=plan).select_related('store', 'store__settings'):   # every shop
        off = enforce(sub.store)
        if off:
            done[sub.store.name] = off
    return done


def is_on(store, key):
    """The module works for this shop right now: allowed by sudo AND (when it has one) its shop switch on."""
    field = MODULES[key]
    settings_obj = getattr(store, 'settings', None) if store is not None else None
    switch_on = True if field is None else bool(getattr(settings_obj, field, False))
    return switch_on and is_allowed(store, key)


def require_on(store, key, detail):
    """Gate for a NEW record of a module (a service job, a transfer…): refused while the module is off."""
    if not is_on(store, key):
        raise ValidationError({'code': 'module_off', 'module': key, 'detail': detail})
