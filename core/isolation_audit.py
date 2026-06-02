"""
Tenant Isolation Audit — the "robot inspector".

A read-only self-test that proves Store A can never see Store B's data. For
every tenant-scoped API endpoint it:

  1. Builds a fake request authenticated as an owner of one real store ("A").
  2. Calls that endpoint's OWN ``get_queryset()`` — the exact same code the
     live API runs — so a forgotten ``store=`` filter is caught here precisely
     as a real client would have exploited it.
  3. Asserts none of the returned rows belong to any other store.

Nothing is written, created, or deleted. Existing store data is reused; we only
ever read and count. The engine lives here (not in a view) so it can be driven
two ways from the same logic:

  - the sudo "Run Check" button  (core.api_admin_isolation.AdminIsolationAuditView)
  - a pre-ship pytest / management command  (catches a leak before it ships)

Each registry entry is ``(label, ViewSetClass, store_lookup)`` where
``store_lookup`` is the ORM path from that endpoint's model to its Store FK
(``'store'`` for direct, ``'invoice__store'`` / ``'product__store'`` for
indirect tenancy).
"""
from rest_framework.test import APIRequestFactory
from rest_framework.request import Request
from rest_framework.generics import GenericAPIView

from django.urls import get_resolver

from users.models import User


# ---------------------------------------------------------------------------
# Auto-discovery: instead of a hand-maintained list (which would re-create the
# exact "someone forgot to add it" bug we're trying to kill), we read the app's
# OWN routing table and test every door it finds. A new endpoint added next
# month shows up here automatically — nothing to remember.
# ---------------------------------------------------------------------------

def _iter_view_classes(resolver=None, prefix=''):
    """Walk the URL conf, yielding (view_class, url_prefix) for every route."""
    resolver = resolver or get_resolver()
    for entry in resolver.url_patterns:
        pat = prefix + str(getattr(entry, 'pattern', ''))
        if hasattr(entry, 'url_patterns'):           # include() — recurse
            yield from _iter_view_classes(entry, pat)
        else:
            cb = entry.callback
            cls = getattr(cb, 'cls', None) or getattr(cb, 'view_class', None)
            if cls is not None:
                yield cls, pat


def _has_superadmin_perm(cls):
    names = {p.__name__ for p in getattr(cls, 'permission_classes', [])}
    return 'IsSuperAdmin' in names


def _derive_store_lookup(model):
    """Find the ORM path from `model` to its Store FK, automatically.
    Returns 'store', a one-hop path like 'invoice__store', or None if the model
    isn't tenant-scoped at all."""
    from core.models import Store
    fks = [f for f in model._meta.get_fields() if getattr(f, 'many_to_one', False)]
    # direct FK to Store
    for f in fks:
        if f.related_model is Store:
            return f.name
    # one hop: a FK whose target itself has a direct Store FK (e.g. Payment→invoice→store)
    for f in fks:
        rel = f.related_model
        if rel is None:
            continue
        for f2 in rel._meta.get_fields():
            if getattr(f2, 'many_to_one', False) and f2.related_model is Store:
                return f'{f.name}__store'
    return None


def _humanize(cls):
    n = cls.__name__
    for suf in ('ViewSet', 'View', 'APIView'):
        if n.endswith(suf):
            n = n[:-len(suf)]
    # camel -> spaced
    out = ''.join((' ' + c) if c.isupper() and i and not n[i-1].isupper() else c
                  for i, c in enumerate(n))
    return out.strip()


def discover_endpoints():
    """Classify every routed endpoint. Returns a dict with:
       tenant   → [(label, cls, lookup)] CRUD endpoints we can probe
       admin    → labels of sudo/global endpoints (intentionally cross-store)
       global   → labels of endpoints whose model has no Store FK
       manual   → labels of endpoints with no get_queryset (custom logic — a
                  human must review these; the probe can't reach them)"""
    tenant, admin, global_, manual = [], [], [], []
    seen = set()
    for cls, path in _iter_view_classes():
        if cls in seen:
            continue
        seen.add(cls)
        label = _humanize(cls)

        # sudo / admin area is meant to span stores — not a leak
        if 'admin' in path or _has_superadmin_perm(cls):
            admin.append(label)
            continue
        # only generic/queryset views can be probed
        if not (isinstance(cls, type) and issubclass(cls, GenericAPIView)):
            continue
        if cls.get_queryset is GenericAPIView.get_queryset and getattr(cls, 'queryset', None) is None:
            manual.append(label)        # custom APIView, no queryset to inspect
            continue

        model = getattr(getattr(cls, 'queryset', None), 'model', None)
        if model is None:
            # need to instantiate to find the model; defer — handled at run time
            tenant.append((label, cls, None))
            continue
        lookup = _derive_store_lookup(model)
        if lookup is None:
            global_.append(label)       # no Store FK → not tenant data
        else:
            tenant.append((label, cls, lookup))
    return {'tenant': tenant, 'admin': admin, 'global': global_, 'manual': manual}


def _probe_request(user):
    """A DRF request that looks authenticated as `user`, for calling
    get_queryset() directly. Never dispatched, never hits the DB to write."""
    drf = Request(APIRequestFactory().get('/__isolation_probe__/'))
    drf.user = user
    return drf


def _check_endpoint(label, ViewSetClass, lookup, store_a, req):
    row = {'endpoint': label, 'store_lookup': lookup}
    try:
        vs = ViewSetClass()
        vs.request = req
        vs.kwargs = {}
        vs.format_kwarg = None
        vs.action = 'list'

        qs = vs.get_queryset()
        model = qs.model

        # discovery may not have known the model yet — derive the path now
        if lookup is None:
            lookup = _derive_store_lookup(model)
            row['store_lookup'] = lookup
        if lookup is None:
            row['status'] = 'no_store_fk'   # not a tenant model after all
            return row

        returned = qs.count()
        # Rows this endpoint handed back that do NOT belong to store A.
        # In a correctly isolated endpoint this is always 0.
        leaked = qs.exclude(**{lookup: store_a}).count()
        # How much OTHER-store data actually exists for this model — so we can
        # tell a real pass ("foreign data existed and was correctly hidden")
        # from a vacuous one ("there was nothing to leak anyway").
        foreign_available = model._default_manager.exclude(**{lookup: store_a}).count()

        row.update(returned=returned, leaked=leaked, foreign_available=foreign_available)
        if leaked > 0:
            row['status'] = 'leak'
        elif foreign_available > 0:
            row['status'] = 'isolated'
        else:
            row['status'] = 'no_foreign_data'
    except Exception as e:  # one bad endpoint must not abort the whole audit
        row['status'] = 'error'
        row['error'] = f'{type(e).__name__}: {e}'
    return row


def _summarize(results, store_a, **extra):
    """Roll a list of per-endpoint rows up into an overall report.

    Overall status:
      - FAIL          → at least one endpoint leaked another store's data
      - PASS          → no leaks, and at least one endpoint was meaningfully
                        tested against real foreign data
      - INCONCLUSIVE  → no leaks, but there was no second store's data to test
                        against (can't prove isolation — add foreign data)
    """
    leaks        = sum(1 for r in results if r['status'] == 'leak')
    isolated     = sum(1 for r in results if r['status'] == 'isolated')
    inconclusive = sum(1 for r in results if r['status'] in ('no_foreign_data', 'no_store_fk'))
    errors       = sum(1 for r in results if r['status'] == 'error')

    if leaks:
        overall = 'FAIL'
    elif isolated:
        overall = 'PASS'
    else:
        overall = 'INCONCLUSIVE'

    report = {
        'status': overall,
        'store_tested': {'id': str(store_a.id), 'name': store_a.name},
        'endpoints_checked': len(results),
        'leaks': leaks,
        'isolated': isolated,
        'inconclusive': inconclusive,
        'errors': errors,
        'endpoints': results,
    }
    report.update(extra)
    return report


def _discovery_meta():
    """The non-tenant doors discovery found — surfaced so they're visible, not
    silently ignored. `manual` especially: custom endpoints the probe can't
    reach that a human should eyeball."""
    d = discover_endpoints()
    return {
        'discovered_total': len(d['tenant']) + len(d['admin']) + len(d['global']) + len(d['manual']),
        'skipped_admin':  sorted(d['admin']),    # sudo/global by design
        'skipped_global': sorted(d['global']),   # no Store FK — not tenant data
        'needs_manual':   sorted(d['manual']),   # custom logic — probe can't test
    }


def run_isolation_audit():
    """Read-only audit against whatever real stores already exist.

    Picks one real store as "A" and verifies every endpoint hides all other
    stores' data. Coverage depends on how much foreign data happens to exist —
    use :func:`run_self_contained_audit` for guaranteed full coverage.
    """
    user_a = (User.objects
              .filter(store__isnull=False, is_superadmin=False, is_active=True)
              .select_related('store')
              .order_by('store__created_at')
              .first())
    if user_a is None:
        return {
            'status': 'INCONCLUSIVE',
            'reason': 'No tenant user found to run the probe as. '
                      'Create a store with at least one user, then re-run.',
            'endpoints': [],
        }

    store_a = user_a.store
    req = _probe_request(user_a)
    tenant = discover_endpoints()['tenant']
    results = [_check_endpoint(label, vs, lookup, store_a, req)
               for label, vs, lookup in tenant]
    return _summarize(results, store_a, mode='live', **_discovery_meta())


# ---------------------------------------------------------------------------
# Self-contained audit: build a throwaway two-store world, test all 23 doors
# against guaranteed foreign data, then roll the whole thing back so nothing
# is ever persisted. Gives a full green/red board independent of real data.
# ---------------------------------------------------------------------------
import datetime as _dt
import uuid as _uuid
from decimal import Decimal as _D

from django.db import transaction
from django.utils.timezone import now as _now

from core.models import Store, Branch, Address, Currency
from inventory.models import (
    Tax, Supplier, Category, AttributeDefinition, Product, ProductVariant,
    StockLevel, StockAdjustment, StockTransfer,
)
from finance.models import (
    PaymentMethod, ExpenseCategory, Expense, SalesInvoice, Payment,
    PurchaseInvoice, RefundInvoice, WorkShift,
)
from users.models import Customer
from notifications.models import Notification
from smart_analysis.models import TablePreset
from billing.models import SubscriptionPlan, Subscription, BillingInvoice


class _Rollback(Exception):
    """Sentinel raised to force the seeding transaction to roll back."""


def _free_store_code(used):
    for n in range(900, 1000):
        c = str(n)
        if c not in used:
            used.add(c)
            return c
    raise RuntimeError('No free 3-digit store_code available for the probe.')


def _make_store(tag, code, currency):
    """An empty store + its OWNER user (the user we can authenticate as)."""
    suffix = _uuid.uuid4().hex[:8]
    owner = User.objects.create(
        username=f'__iso_{tag}_{suffix}', role='OWNER',
        is_superadmin=False, is_active=True,
    )
    store = Store.objects.create(
        name=f'__ISOLATION_PROBE_{tag}__', store_code=code,
        owner=owner, currency=currency,
    )
    owner.store = store
    owner.save(update_fields=['store'])
    return store, owner


def _seed_store_data(store, user):
    """Populate one of every tenant model under `store`. Each row is guarded so
    a single failure degrades that one door to 'no foreign data' instead of
    aborting the whole probe. Returns a list of warning strings."""
    warn = []

    def step(label, fn):
        # Each insert runs in its own savepoint so a single failure rolls back
        # only that row — without it, one IntegrityError poisons the whole
        # outer transaction and every later query dies.
        try:
            with transaction.atomic():
                return fn()
        except Exception as e:
            warn.append(f'{label}: {type(e).__name__}: {e}')
            return None

    addr = step('address', lambda: Address.objects.create(
        store=store, street_1='Probe St', city='Probe'))
    branch = step('branch', lambda: Branch.objects.create(
        store=store, name='Main', address=addr, is_main_branch=True)) if addr else None
    addr2 = step('address2', lambda: Address.objects.create(
        store=store, street_1='Probe St 2', city='Probe'))
    branch2 = step('branch2', lambda: Branch.objects.create(
        store=store, name='Second', address=addr2)) if addr2 else None

    category = step('category', lambda: Category.objects.create(store=store, name='Cat'))
    tax = step('tax', lambda: Tax.objects.create(store=store, name='VAT', rate=_D('0')))
    supplier = step('supplier', lambda: Supplier.objects.create(
        store=store, name='Sup', code_prefix='500', prefix_locked=True))
    step('attribute', lambda: AttributeDefinition.objects.create(
        store=store, name='Color', key='color'))
    pm = step('payment_method', lambda: PaymentMethod.objects.create(store=store, name='Cash'))
    customer = step('customer', lambda: Customer.objects.create(
        store=store, name='Walk-in', phone_number='0100000000'))

    product = step('product', lambda: Product.objects.create(
        store=store, name='Prod', supplier=supplier, category=category, tax=tax,
        base_price=_D('10'))) if supplier else None
    variant = step('variant', lambda: ProductVariant.objects.create(
        product=product, sell_price=_D('10'), cost_price=_D('5'))) if product else None
    if variant and branch:
        step('stock_level', lambda: StockLevel.objects.create(
            variant=variant, branch=branch, quantity=_D('5')))

    expcat = step('expense_category', lambda: ExpenseCategory.objects.create(
        store=store, name='Rent'))
    if branch and expcat:
        step('expense', lambda: Expense.objects.create(
            store=store, branch=branch, category=expcat,
            amount=_D('10'), date=_dt.date.today()))

    invoice = None
    if branch and customer:
        invoice = step('sales_invoice', lambda: SalesInvoice.objects.create(
            store=store, branch=branch, customer=customer,
            status=SalesInvoice.Status.DRAFT, date=_now()))
    if invoice and pm:
        step('payment', lambda: Payment.objects.create(
            invoice=invoice, method=pm, amount=_D('0'), created_by=user))

    if branch and supplier:
        step('purchase', lambda: PurchaseInvoice.objects.create(
            store=store, branch=branch, supplier=supplier,
            status=PurchaseInvoice.Status.DRAFT, date=_now()))
    if branch and customer:
        step('refund', lambda: RefundInvoice.objects.create(
            store=store, branch=branch, customer=customer, created_by=user))
    if branch and variant:
        step('stock_adjustment', lambda: StockAdjustment.objects.create(
            store=store, branch=branch, variant=variant,
            quantity_change=_D('1'), reason=StockAdjustment.Reason.THEFT,
            adjusted_by=user))
    if branch and branch2:
        step('stock_transfer', lambda: StockTransfer.objects.create(
            store=store, from_branch=branch, to_branch=branch2, transferred_by=user))
    if branch:
        step('work_shift', lambda: WorkShift.objects.create(
            store=store, branch=branch, user=user))

    step('notification', lambda: Notification.objects.create(
        store=store, user=user, title='Probe', priority='INFO', type='GENERAL'))
    step('table_preset', lambda: TablePreset.objects.create(
        store=store, table_id='probe', name='Probe', created_by=user))
    step('activity_log', lambda: __import__('core.models', fromlist=['ActivityLog'])
         .ActivityLog.objects.create(store=store, user=user, action='Probe'))

    def _billing():
        plan = SubscriptionPlan.objects.create(name=f'Probe Plan {_uuid.uuid4().hex[:8]}')
        sub = Subscription.objects.create(store=store, plan=plan)
        BillingInvoice.objects.create(
            subscription=sub, store=store, amount=_D('0'),
            invoice_number=f'PROBE-{_uuid.uuid4().hex[:10]}',
            status=BillingInvoice.Status.ISSUED)
    step('billing_invoice', _billing)

    return warn


def run_self_contained_audit():
    """Full-coverage audit. Fabricates an empty Store A (the prober) and a
    fully-populated Store B inside a transaction, verifies A cannot see any of
    B's rows on any endpoint, then rolls everything back. Nothing persists."""
    report = {}
    try:
        with transaction.atomic():
            currency = Currency.objects.first()
            if currency is None:
                currency = Currency.objects.create(
                    code='EGP', symbol='£', name='Egyptian Pound')

            used = set(Store.all_objects.values_list('store_code', flat=True))
            store_a, user_a = _make_store('A', _free_store_code(used), currency)
            store_b, user_b = _make_store('B', _free_store_code(used), currency)
            warnings = _seed_store_data(store_b, user_b)

            req = _probe_request(user_a)
            tenant = discover_endpoints()['tenant']
            results = [_check_endpoint(label, vs, lookup, store_a, req)
                       for label, vs, lookup in tenant]
            report = _summarize(results, store_a, mode='self_contained',
                                seed_warnings=warnings, **_discovery_meta())
            # Don't leave the probe stores behind — undo the entire world.
            raise _Rollback()
    except _Rollback:
        pass
    # Cosmetic: the throwaway store name shouldn't show in the report.
    report['store_tested'] = {'name': 'synthetic Store A (rolled back)'}
    return report
