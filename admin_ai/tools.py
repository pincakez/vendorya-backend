"""Built-in AI tools (Phase C3).

Read / write tools the Admin AI can call via Gemini function-calling.
Only covers features marked ✅ in STATUS.md — POS, returns time-window,
stock transfer, etc. are intentionally absent until those features ship.

Every tool:
  * takes a `ToolContext` as its first positional arg,
  * declares a Gemini-shaped JSON schema in `parameters`,
  * returns plain JSON-serializable Python (dicts / lists / scalars),
  * is small — heavy lifting belongs in serializers / models, not here.

Scoping rules
-------------
`context.store` is the acting store (X-Store-ID header). Tools fall into
three buckets:

  1. Platform-level (no store filter)
       e.g. list_stores, list_admin_users, list_subscription_plans.
       These run with or without an acting store.

  2. Store-scoped reads
       Accept an optional `store_id` override; otherwise use context.store.
       Error out if neither is available.

  3. Store-scoped writes  →  registered with `requires_store=True`.
       The registry refuses to call them when no acting store is set, so
       sudo must "Act As" a store before the AI can mutate its data.
"""
from __future__ import annotations

from datetime import date as _date
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.db import transaction
from django.db.models import Count, Q, Sum, F

from .registry import tool, ToolValidationError


# ============================================================================
#  Helpers
# ============================================================================

_MAX_ROWS = 200       # Hard cap so a runaway tool call can't flood the model.
_DEFAULT_ROWS = 50


def _validate_password(value, user=None):
    """Enforce the full Django password policy on AI-driven password set/reset.

    The AI-admin tool layer must not be a weaker side-door than the normal
    onboarding path — run AUTH_PASSWORD_VALIDATORS, not just a length check.
    """
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError as DjangoValidationError
    try:
        validate_password(value, user)
    except DjangoValidationError as exc:
        raise ToolValidationError(' '.join(exc.messages))


def _clamp_limit(limit: Optional[int]) -> int:
    try:
        n = int(limit) if limit else _DEFAULT_ROWS
    except (TypeError, ValueError):
        n = _DEFAULT_ROWS
    return max(1, min(n, _MAX_ROWS))


def _resolve_store(context, store_id: Optional[str]):
    """Pick the store the read should run against.

    Order: explicit `store_id` arg > context.store. Raises ToolValidationError
    if neither resolves to a real store.
    """
    from core.models import Store
    if store_id:
        store = Store.objects.filter(pk=store_id, is_deleted=False).first()
        if store is None:
            raise ToolValidationError(f"Store {store_id} not found.")
        return store
    if context.store is None:
        raise ToolValidationError(
            "No acting store. Pass `store_id` or set the X-Store-ID header first."
        )
    return context.store


def _dec(value, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ToolValidationError(f"{field!r} must be a decimal number.")


def _parse_date(value, field: str) -> Optional[_date]:
    if value in (None, ''):
        return None
    if isinstance(value, _date):
        return value
    try:
        return _date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ToolValidationError(f"{field!r} must be ISO date (YYYY-MM-DD).")


def _money(d) -> str:
    return str(Decimal(d or 0))


# ============================================================================
#  Context introspection
# ============================================================================

@tool(
    name='get_current_context',
    description='Return the current acting-store context for the Admin AI. '
                'Use when the user asks "where am I" or "what store am I on".',
    parameters={'type': 'object', 'properties': {}},
)
def get_current_context(context):
    store = context.store
    return {
        'acting_store_id': str(store.id) if store else None,
        'acting_store_name': store.name if store else None,
        'is_platform_view': store is None,
        'user': getattr(context.user, 'username', None),
    }


# ============================================================================
#  Platform reads
# ============================================================================

@tool(
    name='list_stores',
    description='List tenant stores on the platform. Sudo-only.',
    parameters={
        'type': 'object',
        'properties': {
            'include_inactive': {'type': 'boolean',
                                 'description': 'Also include deactivated stores.'},
            'search': {'type': 'string',
                       'description': 'Case-insensitive name match.'},
            'limit': {'type': 'integer'},
        },
    },
)
def list_stores(context, include_inactive=False, search=None, limit=None):
    from core.models import Store
    qs = Store.objects.filter(is_deleted=False).select_related('owner', 'currency')
    if not include_inactive:
        qs = qs.filter(is_active=True)
    if search:
        qs = qs.filter(name__icontains=search)
    qs = qs.annotate(
        branches_count=Count('branches', filter=Q(branches__is_deleted=False), distinct=True),
        staff_count=Count('staff', distinct=True),
    ).order_by('name')[:_clamp_limit(limit)]
    return [
        {
            'id': str(s.id),
            'name': s.name,
            'is_active': s.is_active,
            'plan': s.plan,
            'owner': s.owner.username if s.owner_id else None,
            'currency': s.currency.code if s.currency_id else None,
            'timezone': s.timezone,
            'branches': s.branches_count,
            'staff': s.staff_count,
        }
        for s in qs
    ]


@tool(
    name='get_store_info',
    description='Return full info for one store (defaults to the acting store). '
                'Includes owner, plan, currency, timezone, branch + staff counts.',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string', 'description': 'UUID. Optional override.'},
        },
    },
)
def get_store_info(context, store_id=None):
    store = _resolve_store(context, store_id)
    from core.models import Branch
    from users.models import User
    settings = getattr(store, 'settings', None)
    return {
        'id': str(store.id),
        'name': store.name,
        'is_active': store.is_active,
        'plan': store.plan,
        'owner': {
            'id': store.owner_id,
            'username': store.owner.username if store.owner_id else None,
            'email': store.owner.email if store.owner_id else None,
        },
        'currency': {
            'code': store.currency.code if store.currency_id else None,
            'symbol': store.currency.symbol if store.currency_id else None,
        },
        'timezone': store.timezone,
        'default_language': store.default_language,
        'branches_count': Branch.objects.filter(store=store, is_deleted=False).count(),
        'staff_count': User.objects.filter(store=store).count(),
        'settings': {
            'allow_negative_stock': getattr(settings, 'allow_negative_stock', None),
            'enable_agel_selling': getattr(settings, 'enable_agel_selling', None),
            'decimals': getattr(settings, 'decimals', None),
            'tax_id': getattr(settings, 'tax_id', '') if settings else '',
        } if settings else None,
        'created_at': store.created_at.isoformat() if store.created_at else None,
    }


@tool(
    name='get_store_stats',
    description='Today\'s sales total, invoice count, items sold, open shift, and low-stock count '
                'for a store (defaults to acting store). Mirrors the tenant dashboard.',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string', 'description': 'UUID. Optional override.'},
        },
    },
)
def get_store_stats(context, store_id=None):
    from django.utils import timezone as djtz
    from finance.models import SalesInvoice, WorkShift
    from inventory.models import StockLevel

    store = _resolve_store(context, store_id)
    today = djtz.localdate()

    today_invoices = SalesInvoice.objects.filter(
        store=store, status=SalesInvoice.Status.POSTED,
        date__date=today, is_deleted=False,
    )
    agg = today_invoices.aggregate(total=Sum('grand_total'), count=Count('id'))
    items_sold = today_invoices.aggregate(qty=Sum('items__quantity'))['qty'] or 0

    open_shift = WorkShift.objects.filter(store=store, status=WorkShift.Status.OPEN).first()

    low_stock_count = (
        StockLevel.objects
        .filter(branch__store=store, quantity__lte=F('variant__reorder_level'),
                variant__is_deleted=False, variant__product__is_deleted=False)
        .count()
    )

    return {
        'store_id': str(store.id),
        'today_sales_total': _money(agg['total']),
        'today_invoices_count': agg['count'] or 0,
        'today_items_sold': float(items_sold),
        'open_shift': {
            'id': str(open_shift.id),
            'user': open_shift.user.username,
            'starting_cash': _money(open_shift.starting_cash),
        } if open_shift else None,
        'low_stock_count': low_stock_count,
    }


@tool(
    name='list_admin_users',
    description='List Vendorya platform super-admins (sudo accounts).',
    parameters={
        'type': 'object',
        'properties': {
            'include_inactive': {'type': 'boolean'},
            'limit': {'type': 'integer'},
        },
    },
)
def list_admin_users(context, include_inactive=False, limit=None):
    from users.models import User
    qs = User.objects.filter(is_superadmin=True)
    if not include_inactive:
        qs = qs.filter(is_active=True)
    qs = qs.order_by('username')[:_clamp_limit(limit)]
    return [
        {
            'id': u.id,
            'username': u.username,
            'email': u.email,
            'full_name': (f"{u.first_name} {u.last_name}".strip()) or u.username,
            'is_active': u.is_active,
        }
        for u in qs
    ]


@tool(
    name='list_subscription_plans',
    description='List billing plans configured on the platform.',
    parameters={
        'type': 'object',
        'properties': {
            'include_inactive': {'type': 'boolean'},
        },
    },
)
def list_subscription_plans(context, include_inactive=False):
    from billing.models import SubscriptionPlan
    qs = SubscriptionPlan.objects.filter(is_deleted=False)
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return [
        {
            'id': str(p.id),
            'name': p.name,
            'description': p.description,
            'monthly_price': _money(p.monthly_price),
            'annual_price': _money(p.annual_price),
            'currency': p.currency,
            'max_users': p.max_users,
            'max_branches': p.max_branches,
            'max_products': p.max_products,
            'max_invoices_per_month': p.max_invoices_per_month,
            'is_active': p.is_active,
        }
        for p in qs.order_by('monthly_price', 'name')
    ]


@tool(
    name='list_subscriptions',
    description='List tenant subscriptions across the platform.',
    parameters={
        'type': 'object',
        'properties': {
            'status': {'type': 'string',
                       'description': 'TRIAL / ACTIVE / PAST_DUE / CANCELLED'},
            'store_id': {'type': 'string'},
            'limit': {'type': 'integer'},
        },
    },
)
def list_subscriptions(context, status=None, store_id=None, limit=None):
    from billing.models import Subscription
    qs = Subscription.objects.select_related('store', 'plan')
    if status:
        qs = qs.filter(status=status)
    if store_id:
        qs = qs.filter(store_id=store_id)
    qs = qs.order_by('store__name')[:_clamp_limit(limit)]
    return [
        {
            'id': str(s.id),
            'store': {'id': str(s.store_id), 'name': s.store.name},
            'plan_id': str(s.plan_id),
            'plan_name': s.plan.name,
            'display_label': s.display_label,
            'status': s.status,
            'period_start': s.period_start.isoformat() if s.period_start else None,
            'period_end': s.period_end.isoformat() if s.period_end else None,
            'trial_ends_at': s.trial_ends_at.isoformat() if s.trial_ends_at else None,
        }
        for s in qs
    ]


# ============================================================================
#  Per-store reads
# ============================================================================

@tool(
    name='list_branches',
    description='List branches for a store (defaults to acting store).',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
        },
    },
)
def list_branches(context, store_id=None):
    from core.models import Branch
    store = _resolve_store(context, store_id)
    qs = Branch.objects.filter(store=store, is_deleted=False).select_related('address')
    return [
        {
            'id': str(b.id),
            'name': b.name,
            'is_main_branch': b.is_main_branch,
            'address': {
                'street_1': b.address.street_1 if b.address_id else None,
                'city': b.address.city if b.address_id else None,
                'country': b.address.country if b.address_id else None,
            },
        }
        for b in qs.order_by('-is_main_branch', 'name')
    ]


@tool(
    name='list_products',
    description='List products for a store. Supports category_id, supplier_id, '
                'search, low_stock_only, and dynamic attribute filters '
                '(passed as `attrs: {key: value}`, matching AttributeDefinition.key).',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
            'category_id': {'type': 'string'},
            'supplier_id': {'type': 'string'},
            'search': {'type': 'string', 'description': 'Name / SKU / barcode.'},
            'low_stock_only': {'type': 'boolean',
                               'description': 'Only products with any stock level <= 5.'},
            'attrs': {
                'type': 'string',
                'description': 'Dynamic attribute filters as a JSON object string, '
                               'e.g. {"season":"AW25","gender":"Men"}.',
            },
            'limit': {'type': 'integer'},
        },
    },
)
def list_products(context, store_id=None, category_id=None, supplier_id=None,
                  search=None, low_stock_only=False, attrs=None, limit=None):
    from inventory.models import Product
    store = _resolve_store(context, store_id)
    qs = (Product.objects.filter(store=store)
          .select_related('category', 'supplier')
          .prefetch_related('variants', 'variants__stock_levels'))
    if category_id:
        qs = qs.filter(category_id=category_id)
    if supplier_id:
        qs = qs.filter(supplier_id=supplier_id)
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(variants__sku__icontains=search)
            | Q(variants__barcode__icontains=search)
        ).distinct()
    if isinstance(attrs, str) and attrs.strip():
        import json
        try:
            attrs = json.loads(attrs)
        except ValueError:
            attrs = None
    if isinstance(attrs, dict):
        for k, v in attrs.items():
            qs = qs.filter(
                variants__attributes__definition__key=k,
                variants__attributes__value=v,
            ).distinct()

    products = []
    for p in qs.order_by('name')[:_clamp_limit(limit)]:
        variants = list(p.variants.all())
        total_stock = sum(
            (sl.quantity for v in variants for sl in v.stock_levels.all()),
            Decimal('0'),
        )
        if low_stock_only and total_stock > 5:
            continue
        products.append({
            'id': str(p.id),
            'name': p.name,
            'type': p.product_type,
            'category': p.category.name if p.category_id else None,
            'supplier': p.supplier.name if p.supplier_id else None,
            'base_price': _money(p.base_price),
            'variant_count': len(variants),
            'total_stock': str(total_stock),
            'skus': [v.sku for v in variants[:5]],
        })
    return products


@tool(
    name='get_product_detail',
    description='Full product detail including every variant, attribute, and per-branch stock.',
    parameters={
        'type': 'object',
        'properties': {
            'product_id': {'type': 'string'},
            'store_id': {'type': 'string'},
        },
        'required': ['product_id'],
    },
)
def get_product_detail(context, product_id, store_id=None):
    from inventory.models import Product
    store = _resolve_store(context, store_id)
    p = (Product.objects.filter(pk=product_id, store=store)
         .select_related('category', 'supplier', 'tax')
         .prefetch_related(
             'variants', 'variants__stock_levels__branch',
             'variants__attributes__definition',
         )
         .first())
    if p is None:
        raise ToolValidationError(f"Product {product_id} not found in this store.")
    return {
        'id': str(p.id),
        'name': p.name,
        'type': p.product_type,
        'description': p.description or '',
        'unit': p.unit,
        'base_price': _money(p.base_price),
        'category': {'id': str(p.category_id), 'name': p.category.name} if p.category_id else None,
        'supplier': {'id': str(p.supplier_id), 'name': p.supplier.name} if p.supplier_id else None,
        'tax': {'id': str(p.tax_id), 'name': p.tax.name, 'rate': str(p.tax.rate)} if p.tax_id else None,
        'variants': [
            {
                'id': str(v.id),
                'sku': v.sku,
                'barcode': v.barcode or '',
                'cost_price': _money(v.cost_price),
                'sell_price': _money(v.sell_price),
                'attributes': {a.definition.key: a.value for a in v.attributes.all()},
                'stock': [
                    {'branch': sl.branch.name, 'quantity': str(sl.quantity)}
                    for sl in v.stock_levels.all()
                ],
            }
            for v in p.variants.all()
        ],
    }


@tool(
    name='list_categories',
    description='List product categories for a store (with parent if any).',
    parameters={
        'type': 'object',
        'properties': {'store_id': {'type': 'string'}},
    },
)
def list_categories(context, store_id=None):
    from inventory.models import Category
    store = _resolve_store(context, store_id)
    qs = Category.objects.filter(store=store).select_related('parent').order_by('name')
    return [
        {
            'id': str(c.id),
            'name': c.name,
            'parent_id': str(c.parent_id) if c.parent_id else None,
            'parent_name': c.parent.name if c.parent_id else None,
        }
        for c in qs
    ]


@tool(
    name='list_attributes',
    description='List per-store attribute definitions and their option values.',
    parameters={
        'type': 'object',
        'properties': {'store_id': {'type': 'string'}},
    },
)
def list_attributes(context, store_id=None):
    from inventory.models import AttributeDefinition
    store = _resolve_store(context, store_id)
    qs = AttributeDefinition.objects.filter(store=store).order_by('name')
    return [
        {
            'id': str(a.id),
            'name': a.name,
            'key': a.key,
            'input_type': a.input_type,
            'options': a.options or [],
        }
        for a in qs
    ]


@tool(
    name='list_suppliers',
    description='List suppliers for a store, with outstanding balance from unpaid purchases.',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
            'search': {'type': 'string'},
            'limit': {'type': 'integer'},
        },
    },
)
def list_suppliers(context, store_id=None, search=None, limit=None):
    from inventory.models import Supplier
    store = _resolve_store(context, store_id)
    qs = Supplier.objects.filter(store=store)
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(code_prefix__icontains=search))
    qs = qs.order_by('name')[:_clamp_limit(limit)]
    return [
        {
            'id': str(s.id),
            'name': s.name,
            'code_prefix': s.code_prefix,
            'contact_info': s.contact_info or '',
        }
        for s in qs
    ]


@tool(
    name='get_supplier_detail',
    description='Full supplier detail including purchase summary.',
    parameters={
        'type': 'object',
        'properties': {
            'supplier_id': {'type': 'string'},
            'store_id': {'type': 'string'},
        },
        'required': ['supplier_id'],
    },
)
def get_supplier_detail(context, supplier_id, store_id=None):
    from inventory.models import Supplier
    from finance.models import PurchaseInvoice
    store = _resolve_store(context, store_id)
    s = Supplier.objects.filter(pk=supplier_id, store=store).first()
    if s is None:
        raise ToolValidationError(f"Supplier {supplier_id} not found in this store.")
    purchases = PurchaseInvoice.objects.filter(supplier=s, is_deleted=False)
    agg = purchases.aggregate(total=Sum('total_amount'), paid=Sum('paid_amount'))
    return {
        'id': str(s.id),
        'name': s.name,
        'code_prefix': s.code_prefix,
        'contact_info': s.contact_info or '',
        'purchases_count': purchases.count(),
        'purchases_total': _money(agg['total']),
        'purchases_paid': _money(agg['paid']),
        'outstanding_balance': _money(
            (agg['total'] or Decimal('0')) - (agg['paid'] or Decimal('0'))
        ),
    }


@tool(
    name='list_customers',
    description='List customers for a store.',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
            'search': {'type': 'string', 'description': 'Name or phone.'},
            'has_balance': {'type': 'boolean',
                            'description': 'Only customers with non-zero balance.'},
            'limit': {'type': 'integer'},
        },
    },
)
def list_customers(context, store_id=None, search=None, has_balance=False, limit=None):
    from users.models import Customer
    store = _resolve_store(context, store_id)
    qs = Customer.objects.filter(store=store)
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(phone_number__icontains=search))
    if has_balance:
        qs = qs.exclude(balance=0)
    qs = qs.order_by('name')[:_clamp_limit(limit)]
    return [
        {
            'id': str(c.id),
            'name': c.name,
            'phone': c.phone_number,
            'balance': _money(c.balance),
        }
        for c in qs
    ]


@tool(
    name='get_customer_detail',
    description='Customer detail with current balance and recent invoices.',
    parameters={
        'type': 'object',
        'properties': {
            'customer_id': {'type': 'string'},
            'store_id': {'type': 'string'},
        },
        'required': ['customer_id'],
    },
)
def get_customer_detail(context, customer_id, store_id=None):
    from users.models import Customer
    from finance.models import SalesInvoice
    store = _resolve_store(context, store_id)
    c = Customer.objects.filter(pk=customer_id, store=store).first()
    if c is None:
        raise ToolValidationError(f"Customer {customer_id} not found in this store.")
    recent = (SalesInvoice.objects
              .filter(customer=c, is_deleted=False)
              .order_by('-date')[:10])
    return {
        'id': str(c.id),
        'name': c.name,
        'phone': c.phone_number,
        'notes': c.notes or '',
        'balance': _money(c.balance),
        'recent_invoices': [
            {
                'id': str(inv.id),
                'invoice_number': inv.invoice_number,
                'status': inv.status,
                'date': inv.date.isoformat() if inv.date else None,
                'grand_total': _money(inv.grand_total),
                'paid_amount': _money(inv.paid_amount),
            }
            for inv in recent
        ],
    }


@tool(
    name='list_staff',
    description='List staff users for a store (cashier / manager / admin / owner).',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
            'role': {'type': 'string',
                     'description': 'OWNER / ADMIN / MANAGER / CASHIER.'},
            'include_inactive': {'type': 'boolean'},
        },
    },
)
def list_staff(context, store_id=None, role=None, include_inactive=False):
    from users.models import User
    store = _resolve_store(context, store_id)
    qs = User.objects.filter(store=store)
    if role:
        qs = qs.filter(role=role.upper())
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return [
        {
            'id': u.id,
            'username': u.username,
            'full_name': (f"{u.first_name} {u.last_name}".strip()) or u.username,
            'email': u.email,
            'role': u.role,
            'is_active': u.is_active,
        }
        for u in qs.order_by('first_name', 'username')
    ]


@tool(
    name='list_invoices',
    description='List sales invoices for a store, newest first.',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
            'status': {'type': 'string',
                       'description': 'DRAFT / POSTED / VOID'},
            'customer_id': {'type': 'string'},
            'date_from': {'type': 'string', 'description': 'ISO date.'},
            'date_to': {'type': 'string', 'description': 'ISO date.'},
            'limit': {'type': 'integer'},
        },
    },
)
def list_invoices(context, store_id=None, status=None, customer_id=None,
                  date_from=None, date_to=None, limit=None):
    from finance.models import SalesInvoice
    store = _resolve_store(context, store_id)
    qs = (SalesInvoice.objects.filter(store=store, is_deleted=False)
          .select_related('customer', 'branch'))
    if status:
        qs = qs.filter(status=status.upper())
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    df = _parse_date(date_from, 'date_from')
    dt = _parse_date(date_to, 'date_to')
    if df:
        qs = qs.filter(date__date__gte=df)
    if dt:
        qs = qs.filter(date__date__lte=dt)
    qs = qs.order_by('-date')[:_clamp_limit(limit)]
    return [
        {
            'id': str(inv.id),
            'invoice_number': inv.invoice_number,
            'status': inv.status,
            'date': inv.date.isoformat() if inv.date else None,
            'customer': inv.customer.name if inv.customer_id else None,
            'branch': inv.branch.name if inv.branch_id else None,
            'subtotal': _money(inv.subtotal),
            'discount': _money(inv.discount),
            'grand_total': _money(inv.grand_total),
            'paid_amount': _money(inv.paid_amount),
        }
        for inv in qs
    ]


@tool(
    name='list_purchases',
    description='List purchase invoices (incoming stock) for a store.',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
            'status': {'type': 'string',
                       'description': 'DRAFT / RECEIVED'},
            'supplier_id': {'type': 'string'},
            'limit': {'type': 'integer'},
        },
    },
)
def list_purchases(context, store_id=None, status=None, supplier_id=None, limit=None):
    from finance.models import PurchaseInvoice
    store = _resolve_store(context, store_id)
    qs = (PurchaseInvoice.objects.filter(store=store, is_deleted=False)
          .select_related('supplier', 'branch'))
    if status:
        qs = qs.filter(status=status.upper())
    if supplier_id:
        qs = qs.filter(supplier_id=supplier_id)
    qs = qs.order_by('-date')[:_clamp_limit(limit)]
    return [
        {
            'id': str(p.id),
            'supplier': p.supplier.name if p.supplier_id else None,
            'branch': p.branch.name if p.branch_id else None,
            'vendor_reference': p.vendor_reference,
            'status': p.status,
            'date': p.date.isoformat() if p.date else None,
            'total_amount': _money(p.total_amount),
            'paid_amount': _money(p.paid_amount),
        }
        for p in qs
    ]


@tool(
    name='list_expenses',
    description='List expense entries for a store.',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
            'category_id': {'type': 'string'},
            'date_from': {'type': 'string'},
            'date_to': {'type': 'string'},
            'limit': {'type': 'integer'},
        },
    },
)
def list_expenses(context, store_id=None, category_id=None,
                  date_from=None, date_to=None, limit=None):
    from finance.models import Expense
    store = _resolve_store(context, store_id)
    qs = (Expense.objects.filter(store=store, is_deleted=False)
          .select_related('category', 'branch'))
    if category_id:
        qs = qs.filter(category_id=category_id)
    df = _parse_date(date_from, 'date_from')
    dt = _parse_date(date_to, 'date_to')
    if df:
        qs = qs.filter(date__gte=df)
    if dt:
        qs = qs.filter(date__lte=dt)
    qs = qs.order_by('-date')[:_clamp_limit(limit)]
    return [
        {
            'id': str(e.id),
            'category': e.category.name if e.category_id else None,
            'branch': e.branch.name if e.branch_id else None,
            'amount': _money(e.amount),
            'description': e.description,
            'date': e.date.isoformat() if e.date else None,
        }
        for e in qs
    ]


@tool(
    name='list_stock_adjustments',
    description='List stock adjustments (manual stock corrections / theft / damage / gift).',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
            'reason': {'type': 'string',
                       'description': 'THEFT / DAMAGE / CORRECTION / GIFT'},
            'limit': {'type': 'integer'},
        },
    },
)
def list_stock_adjustments(context, store_id=None, reason=None, limit=None):
    from inventory.models import StockAdjustment
    store = _resolve_store(context, store_id)
    qs = (StockAdjustment.objects.filter(store=store)
          .select_related('variant__product', 'branch', 'adjusted_by'))
    if reason:
        qs = qs.filter(reason=reason.upper())
    qs = qs.order_by('-created_at')[:_clamp_limit(limit)]
    return [
        {
            'id': str(a.id),
            'sku': a.variant.sku,
            'product': a.variant.product.name,
            'branch': a.branch.name if a.branch_id else None,
            'quantity_change': str(a.quantity_change),
            'reason': a.reason,
            'notes': a.notes,
            'by': a.adjusted_by.username if a.adjusted_by_id else None,
            'created_at': a.created_at.isoformat() if a.created_at else None,
        }
        for a in qs
    ]


@tool(
    name='get_activity_log',
    description='Read the activity log. When `store_id` is omitted, returns cross-store '
                'entries (sudo only). With `store_id` (or an active acting store), '
                'returns just that store.',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string',
                         'description': 'UUID. Optional — omit for cross-store view.'},
            'operation_type': {'type': 'string',
                               'description': 'SALE / RETURN / PURCHASE / ADJUSTMENT / EXPENSE / SHIFT / STAFF / OTHER'},
            'user_id': {'type': 'integer'},
            'since': {'type': 'string', 'description': 'ISO datetime; only newer entries returned.'},
            'limit': {'type': 'integer'},
        },
    },
)
def get_activity_log(context, store_id=None, operation_type=None,
                     user_id=None, since=None, limit=None):
    from core.models import ActivityLog
    qs = ActivityLog.objects.select_related('user', 'store')

    # store_id explicitly empty/missing AND no acting store = cross-store
    if store_id:
        qs = qs.filter(store_id=store_id)
    elif context.store is not None:
        qs = qs.filter(store=context.store)
    if operation_type:
        qs = qs.filter(operation_type=operation_type.upper())
    if user_id:
        qs = qs.filter(user_id=user_id)
    if since:
        qs = qs.filter(timestamp__gt=since)
    qs = qs.order_by('-timestamp')[:_clamp_limit(limit)]
    return [
        {
            'id': str(log.id),
            'store': log.store.name if log.store_id else None,
            'user': log.user.username if log.user_id else None,
            'operation_type': log.operation_type,
            'action': log.action,
            'details': log.details,
            'timestamp': log.timestamp.isoformat() if log.timestamp else None,
        }
        for log in qs
    ]


# ============================================================================
#  Platform writes
# ============================================================================

@tool(
    name='create_store',
    description='Onboard a new tenant. Atomically creates the owner user, the store, '
                'the main branch + address, and store settings (via signal).',
    parameters={
        'type': 'object',
        'properties': {
            'owner_username': {'type': 'string'},
            'owner_password': {'type': 'string', 'description': 'Min 8 chars.'},
            'owner_email': {'type': 'string'},
            'owner_first_name': {'type': 'string'},
            'owner_last_name': {'type': 'string'},
            'store_name': {'type': 'string'},
            'plan': {'type': 'string', 'description': 'FREE or PREMIUM.'},
            'currency_code': {'type': 'string', 'description': 'e.g. EGP. Optional.'},
            'timezone': {'type': 'string', 'description': 'IANA timezone, defaults to Africa/Cairo.'},
            'default_language': {'type': 'string', 'description': 'e.g. ar, en.'},
            'branch_name': {'type': 'string', 'description': 'Defaults to "Main".'},
            'branch_street_1': {'type': 'string'},
            'branch_street_2': {'type': 'string'},
            'branch_city': {'type': 'string'},
            'branch_country': {'type': 'string', 'description': 'Defaults to "Egypt".'},
        },
        'required': ['owner_username', 'owner_password', 'store_name',
                     'branch_street_1', 'branch_city'],
    },
    write=True,
)
def create_store(context, owner_username, owner_password, store_name,
                 branch_street_1, branch_city,
                 owner_email='', owner_first_name='', owner_last_name='',
                 plan='FREE', currency_code=None,
                 timezone='Africa/Cairo', default_language='ar',
                 branch_name='Main', branch_street_2='',
                 branch_country='Egypt'):
    from core.models import Address, Branch, Currency, Store
    from users.models import User

    _validate_password(owner_password)
    if User.objects.filter(username=owner_username).exists():
        raise ToolValidationError(f"Username {owner_username!r} is already taken.")

    currency = None
    if currency_code:
        currency = Currency.objects.filter(code=currency_code, is_active=True).first()
    if currency is None:
        currency = (Currency.objects.filter(code='EGP').first()
                    or Currency.objects.filter(is_active=True).first())

    plan_value = (plan or 'FREE').upper()
    if plan_value not in dict(Store.SubscriptionPlan.choices):
        raise ToolValidationError(f"Invalid plan {plan!r}.")

    with transaction.atomic():
        owner = User(
            username=owner_username,
            email=owner_email or '',
            first_name=owner_first_name or '',
            last_name=owner_last_name or '',
            role=User.Role.OWNER,
            is_active=True,
            is_superadmin=False,
        )
        owner.set_password(owner_password)
        owner.save()

        store = Store.objects.create(
            owner=owner,
            name=store_name,
            plan=plan_value,
            currency=currency,
            timezone=timezone,
            default_language=default_language,
        )
        owner.store = store
        owner.save(update_fields=['store'])

        address = Address.objects.create(
            store=store,
            street_1=branch_street_1,
            street_2=branch_street_2 or None,
            city=branch_city,
            country=branch_country,
        )
        Branch.objects.create(
            store=store,
            address=address,
            name=branch_name,
            is_main_branch=True,
        )

    return {
        'store_id': str(store.id),
        'store_name': store.name,
        'owner_username': owner.username,
        'owner_id': owner.id,
    }


@tool(
    name='update_store',
    description='Update a store\'s top-level fields (name, plan, timezone, currency, language).',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
            'name': {'type': 'string'},
            'plan': {'type': 'string', 'description': 'FREE or PREMIUM.'},
            'currency_code': {'type': 'string'},
            'timezone': {'type': 'string'},
            'default_language': {'type': 'string'},
        },
        'required': ['store_id'],
    },
    write=True,
)
def update_store(context, store_id, name=None, plan=None, currency_code=None,
                 timezone=None, default_language=None):
    from core.models import Currency, Store
    store = Store.objects.filter(pk=store_id, is_deleted=False).first()
    if store is None:
        raise ToolValidationError(f"Store {store_id} not found.")
    if name is not None:
        store.name = name
    if plan is not None:
        plan_value = plan.upper()
        if plan_value not in dict(Store.SubscriptionPlan.choices):
            raise ToolValidationError(f"Invalid plan {plan!r}.")
        store.plan = plan_value
    if currency_code:
        cur = Currency.objects.filter(code=currency_code, is_active=True).first()
        if cur is None:
            raise ToolValidationError(f"Currency {currency_code!r} not found.")
        store.currency = cur
    if timezone is not None:
        store.timezone = timezone
    if default_language is not None:
        store.default_language = default_language
    store.save()
    return {'ok': True, 'store_id': str(store.id)}


@tool(
    name='toggle_store_active',
    description='Flip a store\'s is_active flag (suspend / unsuspend).',
    parameters={
        'type': 'object',
        'properties': {
            'store_id': {'type': 'string'},
            'is_active': {'type': 'boolean'},
        },
        'required': ['store_id', 'is_active'],
    },
    write=True,
)
def toggle_store_active(context, store_id, is_active):
    from core.models import Store
    store = Store.objects.filter(pk=store_id, is_deleted=False).first()
    if store is None:
        raise ToolValidationError(f"Store {store_id} not found.")
    store.is_active = bool(is_active)
    store.save(update_fields=['is_active', 'updated_at'])
    return {'ok': True, 'store_id': str(store.id), 'is_active': store.is_active}


@tool(
    name='update_branch',
    description='Update a branch\'s name / main-flag / address fields.',
    parameters={
        'type': 'object',
        'properties': {
            'branch_id': {'type': 'string'},
            'name': {'type': 'string'},
            'is_main_branch': {'type': 'boolean'},
            'street_1': {'type': 'string'},
            'street_2': {'type': 'string'},
            'city': {'type': 'string'},
            'country': {'type': 'string'},
        },
        'required': ['branch_id'],
    },
    write=True,
)
def update_branch(context, branch_id, name=None, is_main_branch=None,
                  street_1=None, street_2=None, city=None, country=None):
    from core.models import Branch
    branch = (Branch.objects.filter(pk=branch_id, is_deleted=False)
              .select_related('address').first())
    if branch is None:
        raise ToolValidationError(f"Branch {branch_id} not found.")
    if name is not None:
        branch.name = name
    if is_main_branch is not None:
        branch.is_main_branch = bool(is_main_branch)
    branch.save()

    addr = branch.address
    if addr and any(x is not None for x in (street_1, street_2, city, country)):
        if street_1 is not None:
            addr.street_1 = street_1
        if street_2 is not None:
            addr.street_2 = street_2 or None
        if city is not None:
            addr.city = city
        if country is not None:
            addr.country = country
        addr.save()
    return {'ok': True, 'branch_id': str(branch.id)}


@tool(
    name='update_subscription_plan',
    description='Update a SubscriptionPlan (rename, reprice, change quotas, toggle active).',
    parameters={
        'type': 'object',
        'properties': {
            'plan_id': {'type': 'string'},
            'name': {'type': 'string'},
            'description': {'type': 'string'},
            'monthly_price': {'type': 'string', 'description': 'Decimal as string.'},
            'annual_price': {'type': 'string'},
            'currency': {'type': 'string'},
            'max_users': {'type': 'integer'},
            'max_branches': {'type': 'integer'},
            'max_products': {'type': 'integer'},
            'max_invoices_per_month': {'type': 'integer'},
            'is_active': {'type': 'boolean'},
        },
        'required': ['plan_id'],
    },
    write=True,
)
def update_subscription_plan(context, plan_id, **fields):
    from billing.models import SubscriptionPlan
    plan = SubscriptionPlan.objects.filter(pk=plan_id, is_deleted=False).first()
    if plan is None:
        raise ToolValidationError(f"Plan {plan_id} not found.")
    decimal_fields = {'monthly_price', 'annual_price'}
    for key, value in fields.items():
        if value is None:
            continue
        if key in decimal_fields:
            value = _dec(value, key)
        if not hasattr(plan, key):
            raise ToolValidationError(f"Unknown plan field {key!r}.")
        setattr(plan, key, value)
    plan.save()
    return {'ok': True, 'plan_id': str(plan.id)}


@tool(
    name='update_subscription',
    description='Update a tenant Subscription — switch plan, change status, set period or trial, '
                'override the displayed label, or extend by months.',
    parameters={
        'type': 'object',
        'properties': {
            'subscription_id': {'type': 'string'},
            'plan_id': {'type': 'string'},
            'status': {'type': 'string',
                       'description': 'TRIAL / ACTIVE / PAST_DUE / CANCELLED'},
            'custom_label': {'type': 'string'},
            'period_start': {'type': 'string', 'description': 'ISO date.'},
            'period_end': {'type': 'string', 'description': 'ISO date.'},
            'trial_ends_at': {'type': 'string', 'description': 'ISO date.'},
            'extend_months': {'type': 'integer',
                              'description': 'Add N months to period_end (or to today if unset).'},
            'notes': {'type': 'string'},
        },
        'required': ['subscription_id'],
    },
    write=True,
)
def update_subscription(context, subscription_id, plan_id=None, status=None,
                        custom_label=None, period_start=None, period_end=None,
                        trial_ends_at=None, extend_months=None, notes=None):
    from datetime import timedelta
    from django.utils import timezone as djtz
    from billing.models import Subscription, SubscriptionPlan

    sub = Subscription.objects.filter(pk=subscription_id).first()
    if sub is None:
        raise ToolValidationError(f"Subscription {subscription_id} not found.")

    if plan_id is not None:
        plan = SubscriptionPlan.objects.filter(pk=plan_id, is_deleted=False).first()
        if plan is None:
            raise ToolValidationError(f"Plan {plan_id} not found.")
        sub.plan = plan
    if status is not None:
        status_value = status.upper()
        if status_value not in dict(Subscription.Status.choices):
            raise ToolValidationError(f"Invalid status {status!r}.")
        sub.status = status_value
        if status_value == Subscription.Status.CANCELLED:
            sub.cancelled_at = djtz.now()
    if custom_label is not None:
        sub.custom_label = custom_label
    if period_start is not None:
        sub.period_start = _parse_date(period_start, 'period_start')
    if period_end is not None:
        sub.period_end = _parse_date(period_end, 'period_end')
    if trial_ends_at is not None:
        sub.trial_ends_at = _parse_date(trial_ends_at, 'trial_ends_at')
    if extend_months:
        try:
            months = int(extend_months)
        except (TypeError, ValueError):
            raise ToolValidationError("extend_months must be an integer.")
        # Calendar-naive: add months as ~30-day blocks. Sudo can fine-tune with period_end.
        base = sub.period_end or djtz.localdate()
        sub.period_end = base + timedelta(days=30 * months)
    if notes is not None:
        sub.notes = notes
    sub.save()
    return {
        'ok': True,
        'subscription_id': str(sub.id),
        'status': sub.status,
        'period_end': sub.period_end.isoformat() if sub.period_end else None,
    }


# ============================================================================
#  Per-store writes (require an acting store)
# ============================================================================

@tool(
    name='create_category',
    description='Create a product category in the acting store.',
    parameters={
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'parent_id': {'type': 'string', 'description': 'Optional parent category UUID.'},
        },
        'required': ['name'],
    },
    write=True,
    requires_store=True,
)
def create_category(context, name, parent_id=None):
    from inventory.models import Category
    parent = None
    if parent_id:
        parent = Category.objects.filter(pk=parent_id, store=context.store).first()
        if parent is None:
            raise ToolValidationError(f"Parent category {parent_id} not found.")
    from django.core.exceptions import ValidationError as DjangoValidationError
    try:
        cat = Category.objects.create(store=context.store, name=name, parent=parent)
    except DjangoValidationError as exc:   # depth / cycle guard
        raise ToolValidationError(' '.join(exc.messages))
    return {'ok': True, 'id': str(cat.id), 'name': cat.name}


@tool(
    name='update_category',
    description='Rename or re-parent a category.',
    parameters={
        'type': 'object',
        'properties': {
            'category_id': {'type': 'string'},
            'name': {'type': 'string'},
            'parent_id': {'type': 'string', 'description': 'Empty string to clear parent.'},
        },
        'required': ['category_id'],
    },
    write=True,
    requires_store=True,
)
def update_category(context, category_id, name=None, parent_id=None):
    from inventory.models import Category
    cat = Category.objects.filter(pk=category_id, store=context.store).first()
    if cat is None:
        raise ToolValidationError(f"Category {category_id} not found.")
    if name is not None:
        cat.name = name
    if parent_id is not None:
        if parent_id == '':
            cat.parent = None
        else:
            parent = Category.objects.filter(pk=parent_id, store=context.store).first()
            if parent is None:
                raise ToolValidationError(f"Parent category {parent_id} not found.")
            cat.parent = parent
    from django.core.exceptions import ValidationError as DjangoValidationError
    try:
        cat.save()
    except DjangoValidationError as exc:   # depth / cycle guard
        raise ToolValidationError(' '.join(exc.messages))
    return {'ok': True, 'id': str(cat.id)}


@tool(
    name='create_attribute',
    description='Create an attribute definition (e.g. Color, Size, Season) for the acting store.',
    parameters={
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'key': {'type': 'string', 'description': 'Slug. Optional — derived from name.'},
            'input_type': {'type': 'string',
                           'description': 'TEXT / SELECT / NUMBER'},
            'options': {'type': 'array', 'items': {'type': 'string'},
                        'description': 'For SELECT only.'},
        },
        'required': ['name'],
    },
    write=True,
    requires_store=True,
)
def create_attribute(context, name, key=None, input_type='TEXT', options=None):
    from django.utils.text import slugify
    from inventory.models import AttributeDefinition
    input_type_value = (input_type or 'TEXT').upper()
    if input_type_value not in dict(AttributeDefinition.InputType.choices):
        raise ToolValidationError(f"Invalid input_type {input_type!r}.")
    attr = AttributeDefinition.objects.create(
        store=context.store,
        name=name,
        key=key or slugify(name).replace('-', '_'),
        input_type=input_type_value,
        options=list(options or []),
    )
    return {'ok': True, 'id': str(attr.id), 'key': attr.key}


@tool(
    name='update_attribute',
    description='Update an attribute definition (name, input_type, or full options list).',
    parameters={
        'type': 'object',
        'properties': {
            'attribute_id': {'type': 'string'},
            'name': {'type': 'string'},
            'input_type': {'type': 'string'},
            'options': {'type': 'array', 'items': {'type': 'string'}},
        },
        'required': ['attribute_id'],
    },
    write=True,
    requires_store=True,
)
def update_attribute(context, attribute_id, name=None, input_type=None, options=None):
    from inventory.models import AttributeDefinition
    attr = AttributeDefinition.objects.filter(pk=attribute_id, store=context.store).first()
    if attr is None:
        raise ToolValidationError(f"Attribute {attribute_id} not found.")
    if name is not None:
        attr.name = name
    if input_type is not None:
        v = input_type.upper()
        if v not in dict(AttributeDefinition.InputType.choices):
            raise ToolValidationError(f"Invalid input_type {input_type!r}.")
        attr.input_type = v
    if options is not None:
        attr.options = list(options)
    attr.save()
    return {'ok': True, 'id': str(attr.id)}


@tool(
    name='bulk_update_attribute_value',
    description='Find/replace on an attribute\'s option values across the store. '
                'Renames the option in AttributeDefinition.options AND updates every '
                'ProductAttribute row currently set to old_value.',
    parameters={
        'type': 'object',
        'properties': {
            'attribute_id': {'type': 'string'},
            'old_value': {'type': 'string'},
            'new_value': {'type': 'string'},
        },
        'required': ['attribute_id', 'old_value', 'new_value'],
    },
    write=True,
    requires_store=True,
)
def bulk_update_attribute_value(context, attribute_id, old_value, new_value):
    from inventory.models import AttributeDefinition, ProductAttribute
    attr = AttributeDefinition.objects.filter(pk=attribute_id, store=context.store).first()
    if attr is None:
        raise ToolValidationError(f"Attribute {attribute_id} not found.")
    with transaction.atomic():
        if isinstance(attr.options, list):
            attr.options = [new_value if o == old_value else o for o in attr.options]
            attr.save(update_fields=['options', 'updated_at'])
        rows_changed = (
            ProductAttribute.objects
            .filter(definition=attr, value=old_value,
                    variant__product__store=context.store)
            .update(value=new_value)
        )
    return {'ok': True, 'attribute_id': str(attr.id),
            'rows_changed': rows_changed}


@tool(
    name='create_supplier',
    description='Create a supplier in the acting store. code_prefix must be 2 unique digits.',
    parameters={
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'code_prefix': {'type': 'string', 'description': '2 digits, e.g. "13".'},
            'contact_info': {'type': 'string'},
        },
        'required': ['name', 'code_prefix'],
    },
    write=True,
    requires_store=True,
)
def create_supplier(context, name, code_prefix, contact_info=''):
    from django.core.exceptions import ValidationError as DjValidation
    from inventory.models import Supplier
    sup = Supplier(store=context.store, name=name,
                   code_prefix=code_prefix, contact_info=contact_info or '')
    try:
        sup.full_clean()
        sup.save()
    except DjValidation as e:
        raise ToolValidationError(f"Validation failed: {e.message_dict}")
    return {'ok': True, 'id': str(sup.id), 'code_prefix': sup.code_prefix}


@tool(
    name='update_supplier',
    description='Update a supplier\'s fields.',
    parameters={
        'type': 'object',
        'properties': {
            'supplier_id': {'type': 'string'},
            'name': {'type': 'string'},
            'code_prefix': {'type': 'string'},
            'contact_info': {'type': 'string'},
        },
        'required': ['supplier_id'],
    },
    write=True,
    requires_store=True,
)
def update_supplier(context, supplier_id, name=None, code_prefix=None, contact_info=None):
    from inventory.models import Supplier
    sup = Supplier.objects.filter(pk=supplier_id, store=context.store).first()
    if sup is None:
        raise ToolValidationError(f"Supplier {supplier_id} not found.")
    if name is not None:
        sup.name = name
    if code_prefix is not None:
        sup.code_prefix = code_prefix
    if contact_info is not None:
        sup.contact_info = contact_info
    sup.save()
    return {'ok': True, 'id': str(sup.id)}


@tool(
    name='create_customer',
    description='Create a customer in the acting store. Phone must be unique per store.',
    parameters={
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'phone_number': {'type': 'string'},
            'notes': {'type': 'string'},
            'opening_balance': {'type': 'string',
                                'description': 'Decimal; positive = owes us.'},
        },
        'required': ['name', 'phone_number'],
    },
    write=True,
    requires_store=True,
)
def create_customer(context, name, phone_number, notes='', opening_balance=None):
    from users.models import Customer
    if Customer.objects.filter(store=context.store, phone_number=phone_number).exists():
        raise ToolValidationError(f"Phone {phone_number!r} already used in this store.")
    customer = Customer.objects.create(
        store=context.store,
        name=name,
        phone_number=phone_number,
        notes=notes or '',
        balance=_dec(opening_balance, 'opening_balance') if opening_balance else Decimal('0.00'),
    )
    return {'ok': True, 'id': str(customer.id)}


@tool(
    name='update_customer',
    description='Update a customer\'s name / phone / notes.',
    parameters={
        'type': 'object',
        'properties': {
            'customer_id': {'type': 'string'},
            'name': {'type': 'string'},
            'phone_number': {'type': 'string'},
            'notes': {'type': 'string'},
        },
        'required': ['customer_id'],
    },
    write=True,
    requires_store=True,
)
def update_customer(context, customer_id, name=None, phone_number=None, notes=None):
    from users.models import Customer
    c = Customer.objects.filter(pk=customer_id, store=context.store).first()
    if c is None:
        raise ToolValidationError(f"Customer {customer_id} not found.")
    if name is not None:
        c.name = name
    if phone_number is not None:
        if (Customer.objects.filter(store=context.store, phone_number=phone_number)
                .exclude(pk=c.pk).exists()):
            raise ToolValidationError(f"Phone {phone_number!r} already used in this store.")
        c.phone_number = phone_number
    if notes is not None:
        c.notes = notes
    c.save()
    return {'ok': True, 'id': str(c.id)}


@tool(
    name='create_product',
    description='Create a product with one initial variant. For multiple variants, '
                'call this then use update_product to add more later.',
    parameters={
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'product_type': {'type': 'string',
                             'description': 'STANDARD / SERVICE / BUNDLE'},
            'category_id': {'type': 'string'},
            'supplier_id': {'type': 'string'},
            'tax_id': {'type': 'string'},
            'description': {'type': 'string'},
            'unit': {'type': 'string', 'description': 'Defaults to "pcs".'},
            'base_price': {'type': 'string', 'description': 'Decimal.'},
            'variant_sku': {'type': 'string', 'description': 'Optional — auto-generated if blank.'},
            'variant_barcode': {'type': 'string'},
            'variant_cost_price': {'type': 'string'},
            'variant_sell_price': {'type': 'string'},
        },
        'required': ['name'],
    },
    write=True,
    requires_store=True,
)
def create_product(context, name, product_type='STANDARD', category_id=None,
                   supplier_id=None, tax_id=None, description='', unit='pcs',
                   base_price='0', variant_sku=None, variant_barcode=None,
                   variant_cost_price='0', variant_sell_price='0'):
    from inventory.models import Category, Product, ProductVariant, Supplier, Tax

    pt = (product_type or 'STANDARD').upper()
    if pt not in dict(Product.ProductType.choices):
        raise ToolValidationError(f"Invalid product_type {product_type!r}.")

    def _lookup(model, pk, label):
        if not pk:
            return None
        obj = model.objects.filter(pk=pk, store=context.store).first()
        if obj is None:
            raise ToolValidationError(f"{label} {pk} not found in this store.")
        return obj

    category = _lookup(Category, category_id, 'Category')
    supplier = _lookup(Supplier, supplier_id, 'Supplier')
    tax = _lookup(Tax, tax_id, 'Tax')

    with transaction.atomic():
        product = Product.objects.create(
            store=context.store,
            name=name,
            product_type=pt,
            category=category,
            supplier=supplier,
            tax=tax,
            description=description or '',
            unit=unit or 'pcs',
            base_price=_dec(base_price, 'base_price'),
        )
        variant = ProductVariant(
            product=product,
            sku=variant_sku or '',
            barcode=variant_barcode or None,
            cost_price=_dec(variant_cost_price, 'variant_cost_price'),
            sell_price=_dec(variant_sell_price, 'variant_sell_price'),
        )
        variant.save()  # signal/auto-SKU
    return {
        'ok': True,
        'product_id': str(product.id),
        'variant_id': str(variant.id),
        'sku': variant.sku,
    }


@tool(
    name='update_product',
    description='Update a product\'s top-level fields (name, category, supplier, tax, '
                'description, base_price, unit).',
    parameters={
        'type': 'object',
        'properties': {
            'product_id': {'type': 'string'},
            'name': {'type': 'string'},
            'category_id': {'type': 'string', 'description': 'Empty string to clear.'},
            'supplier_id': {'type': 'string', 'description': 'Empty string to clear.'},
            'tax_id': {'type': 'string', 'description': 'Empty string to clear.'},
            'description': {'type': 'string'},
            'unit': {'type': 'string'},
            'base_price': {'type': 'string'},
        },
        'required': ['product_id'],
    },
    write=True,
    requires_store=True,
)
def update_product(context, product_id, name=None, category_id=None, supplier_id=None,
                   tax_id=None, description=None, unit=None, base_price=None):
    from inventory.models import Category, Product, Supplier, Tax
    product = Product.objects.filter(pk=product_id, store=context.store).first()
    if product is None:
        raise ToolValidationError(f"Product {product_id} not found.")

    def _set_fk(field, value, model, label):
        if value is None:
            return
        if value == '':
            setattr(product, field, None)
            return
        obj = model.objects.filter(pk=value, store=context.store).first()
        if obj is None:
            raise ToolValidationError(f"{label} {value} not found in this store.")
        setattr(product, field, obj)

    if name is not None:
        product.name = name
    _set_fk('category', category_id, Category, 'Category')
    _set_fk('supplier', supplier_id, Supplier, 'Supplier')
    _set_fk('tax', tax_id, Tax, 'Tax')
    if description is not None:
        product.description = description
    if unit is not None:
        product.unit = unit
    if base_price is not None:
        product.base_price = _dec(base_price, 'base_price')
    product.save()
    return {'ok': True, 'product_id': str(product.id)}


@tool(
    name='create_staff_user',
    description='Add a staff user to the acting store.',
    parameters={
        'type': 'object',
        'properties': {
            'username': {'type': 'string'},
            'password': {'type': 'string', 'description': 'Min 8 chars.'},
            'role': {'type': 'string',
                     'description': 'OWNER / ADMIN / MANAGER / CASHIER. Defaults to CASHIER.'},
            'first_name': {'type': 'string'},
            'last_name': {'type': 'string'},
            'email': {'type': 'string'},
        },
        'required': ['username', 'password'],
    },
    write=True,
    requires_store=True,
)
def create_staff_user(context, username, password, role='CASHIER',
                      first_name='', last_name='', email=''):
    from users.models import User
    _validate_password(password)
    if User.objects.filter(username=username).exists():
        raise ToolValidationError(f"Username {username!r} already taken.")
    role_value = (role or 'CASHIER').upper()
    if role_value not in dict(User.Role.choices):
        raise ToolValidationError(f"Invalid role {role!r}.")
    user = User(
        username=username,
        email=email or '',
        first_name=first_name or '',
        last_name=last_name or '',
        store=context.store,
        role=role_value,
        is_active=True,
        is_superadmin=False,
    )
    user.set_password(password)
    user.save()
    return {'ok': True, 'user_id': user.id, 'username': user.username, 'role': user.role}


@tool(
    name='update_staff_user',
    description='Update a staff user\'s name, email, role, or password.',
    parameters={
        'type': 'object',
        'properties': {
            'user_id': {'type': 'integer'},
            'first_name': {'type': 'string'},
            'last_name': {'type': 'string'},
            'email': {'type': 'string'},
            'role': {'type': 'string'},
            'password': {'type': 'string', 'description': 'New password (min 8). Optional.'},
        },
        'required': ['user_id'],
    },
    write=True,
    requires_store=True,
)
def update_staff_user(context, user_id, first_name=None, last_name=None,
                      email=None, role=None, password=None):
    from users.models import User
    user = User.objects.filter(pk=user_id, store=context.store).first()
    if user is None:
        raise ToolValidationError(f"Staff user {user_id} not found in this store.")
    if first_name is not None:
        user.first_name = first_name
    if last_name is not None:
        user.last_name = last_name
    if email is not None:
        user.email = email
    if role is not None:
        role_value = role.upper()
        if role_value not in dict(User.Role.choices):
            raise ToolValidationError(f"Invalid role {role!r}.")
        user.role = role_value
    if password:
        _validate_password(password, user)
        user.set_password(password)
    user.save()
    return {'ok': True, 'user_id': user.id}


@tool(
    name='deactivate_staff_user',
    description='Set a staff user\'s is_active flag to false.',
    parameters={
        'type': 'object',
        'properties': {
            'user_id': {'type': 'integer'},
            'reactivate': {'type': 'boolean',
                           'description': 'Pass true to instead re-enable a disabled user.'},
        },
        'required': ['user_id'],
    },
    write=True,
    requires_store=True,
)
def deactivate_staff_user(context, user_id, reactivate=False):
    from users.models import User
    user = User.objects.filter(pk=user_id, store=context.store).first()
    if user is None:
        raise ToolValidationError(f"Staff user {user_id} not found in this store.")
    user.is_active = bool(reactivate)
    user.save(update_fields=['is_active'])
    return {'ok': True, 'user_id': user.id, 'is_active': user.is_active}


@tool(
    name='create_purchase_invoice',
    description='Create a DRAFT purchase invoice (incoming stock). Call receive_purchase '
                'to move it to RECEIVED and increment stock.',
    parameters={
        'type': 'object',
        'properties': {
            'supplier_id': {'type': 'string'},
            'branch_id': {'type': 'string'},
            'date': {'type': 'string', 'description': 'ISO datetime; defaults to now.'},
            'vendor_reference': {'type': 'string'},
            'notes': {'type': 'string'},
            'items': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'variant_id': {'type': 'string'},
                        'quantity': {'type': 'string'},
                        'unit_cost': {'type': 'string'},
                    },
                    'required': ['variant_id', 'quantity', 'unit_cost'],
                },
            },
        },
        'required': ['supplier_id', 'branch_id', 'items'],
    },
    write=True,
    requires_store=True,
)
def create_purchase_invoice(context, supplier_id, branch_id, items,
                            date=None, vendor_reference='', notes=''):
    from django.utils import timezone as djtz
    from core.models import Branch
    from finance.models import PurchaseInvoice, PurchaseItem
    from inventory.models import ProductVariant, Supplier

    supplier = Supplier.objects.filter(pk=supplier_id, store=context.store).first()
    if supplier is None:
        raise ToolValidationError(f"Supplier {supplier_id} not found.")
    branch = Branch.objects.filter(pk=branch_id, store=context.store).first()
    if branch is None:
        raise ToolValidationError(f"Branch {branch_id} not found in this store.")
    if not items:
        raise ToolValidationError("items must contain at least one row.")

    when = djtz.now()
    if date:
        # Accept ISO date or full datetime.
        from django.utils.dateparse import parse_datetime, parse_date as _pd
        when = parse_datetime(date) or _pd(date)
        if not when:
            raise ToolValidationError("date must be ISO date or datetime.")

    with transaction.atomic():
        invoice = PurchaseInvoice.objects.create(
            store=context.store,
            supplier=supplier,
            branch=branch,
            date=when,
            vendor_reference=vendor_reference or '',
            notes=notes or '',
            status=PurchaseInvoice.Status.DRAFT,
        )
        for row in items:
            variant = ProductVariant.objects.filter(
                pk=row.get('variant_id'),
                product__store=context.store,
            ).first()
            if variant is None:
                raise ToolValidationError(f"Variant {row.get('variant_id')} not found.")
            PurchaseItem.objects.create(
                invoice=invoice,
                variant=variant,
                quantity=_dec(row.get('quantity'), 'quantity'),
                unit_cost=_dec(row.get('unit_cost'), 'unit_cost'),
            )
    invoice.refresh_from_db()
    return {
        'ok': True,
        'purchase_id': str(invoice.id),
        'total_amount': _money(invoice.total_amount),
        'status': invoice.status,
    }


@tool(
    name='receive_purchase',
    description='Mark a DRAFT purchase invoice as RECEIVED — this increments stock and '
                'updates variant cost_price via the existing signal.',
    parameters={
        'type': 'object',
        'properties': {
            'purchase_id': {'type': 'string'},
        },
        'required': ['purchase_id'],
    },
    write=True,
    requires_store=True,
)
def receive_purchase(context, purchase_id):
    from finance.models import PurchaseInvoice
    invoice = PurchaseInvoice.objects.filter(
        pk=purchase_id, store=context.store, is_deleted=False,
    ).first()
    if invoice is None:
        raise ToolValidationError(f"Purchase {purchase_id} not found.")
    if invoice.status != PurchaseInvoice.Status.DRAFT:
        raise ToolValidationError("Only DRAFT purchases can be received.")
    invoice.status = PurchaseInvoice.Status.RECEIVED
    invoice.save()
    return {'ok': True, 'purchase_id': str(invoice.id), 'status': invoice.status}


@tool(
    name='create_sales_invoice',
    description='Create a sales invoice. Pass status=POSTED to immediately decrement stock '
                'and assign an invoice number; status=DRAFT to save without posting.',
    parameters={
        'type': 'object',
        'properties': {
            'branch_id': {'type': 'string'},
            'customer_id': {'type': 'string'},
            'date': {'type': 'string', 'description': 'ISO datetime; defaults to now.'},
            'status': {'type': 'string', 'description': 'DRAFT or POSTED. Default DRAFT.'},
            'discount': {'type': 'string'},
            'items': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'variant_id': {'type': 'string'},
                        'quantity': {'type': 'string'},
                        'unit_price': {'type': 'string'},
                        'tax_amount': {'type': 'string'},
                    },
                    'required': ['variant_id', 'quantity', 'unit_price'],
                },
            },
        },
        'required': ['branch_id', 'customer_id', 'items'],
    },
    write=True,
    requires_store=True,
)
def create_sales_invoice(context, branch_id, customer_id, items,
                         date=None, status='DRAFT', discount='0'):
    from django.utils import timezone as djtz
    from core.models import Branch
    from finance.models import SalesInvoice, SalesInvoiceItem
    from inventory.models import ProductVariant
    from users.models import Customer

    branch = Branch.objects.filter(pk=branch_id, store=context.store).first()
    if branch is None:
        raise ToolValidationError(f"Branch {branch_id} not found in this store.")
    customer = Customer.objects.filter(pk=customer_id, store=context.store).first()
    if customer is None:
        raise ToolValidationError(f"Customer {customer_id} not found in this store.")
    if not items:
        raise ToolValidationError("items must contain at least one row.")
    status_value = (status or 'DRAFT').upper()
    if status_value not in (SalesInvoice.Status.DRAFT, SalesInvoice.Status.POSTED):
        raise ToolValidationError(
            "status must be DRAFT or POSTED (use the void tool / endpoint to cancel).",
        )

    when = djtz.now()
    if date:
        from django.utils.dateparse import parse_datetime, parse_date as _pd
        when = parse_datetime(date) or _pd(date)
        if not when:
            raise ToolValidationError("date must be ISO date or datetime.")

    with transaction.atomic():
        invoice = SalesInvoice.objects.create(
            store=context.store,
            branch=branch,
            customer=customer,
            date=when,
            discount=_dec(discount, 'discount'),
            status=SalesInvoice.Status.DRAFT,  # always draft first so item signal can fire
        )
        subtotal = Decimal('0')
        tax_total = Decimal('0')
        for row in items:
            variant = ProductVariant.objects.filter(
                pk=row.get('variant_id'), product__store=context.store,
            ).first()
            if variant is None:
                raise ToolValidationError(f"Variant {row.get('variant_id')} not found.")
            qty = _dec(row.get('quantity'), 'quantity')
            unit_price = _dec(row.get('unit_price'), 'unit_price')
            tax_amount = _dec(row.get('tax_amount') or '0', 'tax_amount')
            SalesInvoiceItem.objects.create(
                invoice=invoice, variant=variant,
                quantity=qty, unit_price=unit_price, tax_amount=tax_amount,
            )
            subtotal += qty * unit_price
            tax_total += tax_amount
        invoice.subtotal = subtotal
        invoice.tax_total = tax_total
        invoice.grand_total = subtotal + tax_total - invoice.discount
        invoice.save(update_fields=['subtotal', 'tax_total', 'grand_total'])

        if status_value == SalesInvoice.Status.POSTED:
            invoice.status = SalesInvoice.Status.POSTED
            invoice.save()  # triggers pre_save signal → stock decrement

    invoice.refresh_from_db()
    return {
        'ok': True,
        'invoice_id': str(invoice.id),
        'invoice_number': invoice.invoice_number,
        'status': invoice.status,
        'grand_total': _money(invoice.grand_total),
    }


@tool(
    name='create_expense',
    description='Record an expense in the acting store.',
    parameters={
        'type': 'object',
        'properties': {
            'branch_id': {'type': 'string'},
            'category_id': {'type': 'string'},
            'amount': {'type': 'string'},
            'date': {'type': 'string', 'description': 'ISO date. Defaults to today.'},
            'description': {'type': 'string'},
        },
        'required': ['branch_id', 'category_id', 'amount'],
    },
    write=True,
    requires_store=True,
)
def create_expense(context, branch_id, category_id, amount, date=None, description=''):
    from django.utils import timezone as djtz
    from core.models import Branch
    from finance.models import Expense, ExpenseCategory

    branch = Branch.objects.filter(pk=branch_id, store=context.store).first()
    if branch is None:
        raise ToolValidationError(f"Branch {branch_id} not found.")
    category = ExpenseCategory.objects.filter(
        pk=category_id, store=context.store, is_deleted=False,
    ).first()
    if category is None:
        raise ToolValidationError(f"Expense category {category_id} not found.")
    when = _parse_date(date, 'date') or djtz.localdate()
    expense = Expense.objects.create(
        store=context.store, branch=branch, category=category,
        amount=_dec(amount, 'amount'),
        description=description or '',
        date=when,
    )
    return {'ok': True, 'expense_id': str(expense.id),
            'amount': _money(expense.amount)}


@tool(
    name='create_stock_adjustment',
    description='Manual stock correction. Positive quantity_change = stock gained, negative = loss.',
    parameters={
        'type': 'object',
        'properties': {
            'variant_id': {'type': 'string'},
            'branch_id': {'type': 'string'},
            'quantity_change': {'type': 'string',
                                'description': 'Signed decimal, e.g. "-3" for 3 units lost.'},
            'reason': {'type': 'string',
                       'description': 'THEFT / DAMAGE / CORRECTION / GIFT'},
            'notes': {'type': 'string'},
        },
        'required': ['variant_id', 'branch_id', 'quantity_change', 'reason'],
    },
    write=True,
    requires_store=True,
)
def create_stock_adjustment(context, variant_id, branch_id, quantity_change,
                            reason, notes=''):
    from core.models import Branch
    from inventory.models import ProductVariant, StockAdjustment

    variant = ProductVariant.objects.filter(
        pk=variant_id, product__store=context.store,
    ).first()
    if variant is None:
        raise ToolValidationError(f"Variant {variant_id} not found in this store.")
    branch = Branch.objects.filter(pk=branch_id, store=context.store).first()
    if branch is None:
        raise ToolValidationError(f"Branch {branch_id} not found in this store.")
    reason_value = reason.upper()
    if reason_value not in dict(StockAdjustment.Reason.choices):
        raise ToolValidationError(f"Invalid reason {reason!r}.")

    from django.core.exceptions import ValidationError as DjangoValidationError
    try:
        adjustment = StockAdjustment.objects.create(
            store=context.store,
            branch=branch,
            variant=variant,
            quantity_change=_dec(quantity_change, 'quantity_change'),
            reason=reason_value,
            notes=notes or '',
            adjusted_by=context.user,
        )
    except DjangoValidationError as exc:
        # Negative-stock policy block (raised in StockAdjustment.save).
        raise ToolValidationError(' '.join(exc.messages))
    return {
        'ok': True,
        'adjustment_id': str(adjustment.id),
        'sku': variant.sku,
        'change': str(adjustment.quantity_change),
    }


@tool(
    name='send_in_app_notification',
    description='Drop an in-app notification into the bell inbox for a store (or one specific user). '
                'Used to message tenants without email.',
    parameters={
        'type': 'object',
        'properties': {
            'title': {'type': 'string'},
            'body': {'type': 'string'},
            'type': {'type': 'string',
                     'description': 'BILLING_INVOICE / BILLING_PAID / SUBSCRIPTION / '
                                    'LOW_STOCK / SHIFT_DIFFERENCE / SYSTEM / GENERAL'},
            'user_id': {'type': 'integer',
                        'description': 'Optional — narrow to one staff user.'},
            'link': {'type': 'string', 'description': 'Frontend route to deep-link.'},
            'payload': {'type': 'object', 'description': 'Optional JSON metadata.'},
        },
        'required': ['title'],
    },
    write=True,
    requires_store=True,
)
def send_in_app_notification(context, title, body='', type='GENERAL',
                             user_id=None, link='', payload=None):
    from notifications.models import Notification
    from users.models import User

    type_value = (type or 'GENERAL').upper()
    if type_value not in dict(Notification.Type.choices):
        raise ToolValidationError(f"Invalid notification type {type!r}.")

    user = None
    if user_id:
        user = User.objects.filter(pk=user_id, store=context.store).first()
        if user is None:
            raise ToolValidationError(f"User {user_id} not found in this store.")

    note = Notification.objects.create(
        store=context.store,
        user=user,
        type=type_value,
        title=title,
        body=body or '',
        link=link or '',
        payload=payload or {},
    )
    return {'ok': True, 'notification_id': str(note.id),
            'target': 'user' if user else 'store-wide'}
