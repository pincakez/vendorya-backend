"""Dashboard widget catalog — the single source of truth for which widgets
may appear on a store dashboard, and the platform default selection.

Pass 2 (§W): sudo picks a GLOBAL set of ≤ MAX_WIDGETS widgets that every store
shows. Per-store overrides can layer on top later (DashboardLayout.store FK).

Keep WIDGET_CATALOG in sync with the frontend gallery ids
(`vendorya-frontend/src/views/admin/WidgetGallery.vue`).
"""

MAX_WIDGETS = 9

# The renderable, selectable widgets (id → human label, for reference/validation).
# Order here is the default dashboard order.
WIDGET_CATALOG = [
    'today-sales',     # Today's Sales KPI
    'weekly-revenue',  # last-7-days revenue area chart
    'recent-sales',    # recent invoices table
    'low-stock-list',  # items below reorder level
    'services',        # upcoming services
    'stock-health',    # stock status chips
    'revenue-ring',    # today vs daily-average gauge
    'activity-feed',   # recent store events
    'heat-calendar',   # monthly sales heatmap
]

# Shown out of the box (all of them — exactly MAX_WIDGETS).
DEFAULT_WIDGETS = list(WIDGET_CATALOG)


def sanitize(ids):
    """Return a valid, de-duped, order-preserving, capped widget-id list."""
    seen, out = set(), []
    for wid in (ids or []):
        if wid in WIDGET_CATALOG and wid not in seen:
            seen.add(wid)
            out.append(wid)
        if len(out) >= MAX_WIDGETS:
            break
    return out
