"""`/api/treasury/owner/` — the owner level (TREASURY.md § 12). Part A registers under "part A", part B under "part B"
(separate comment blocks → the two agents never edit the same lines)."""
from rest_framework.routers import DefaultRouter

router = DefaultRouter()

# ── part A (owner_views) ──
from .owner_views import (  # noqa: E402
    OverviewViewSet, OwnerPartViewSet, OwnerBoxViewSet, OwnerMoveViewSet, OwnerLineViewSet, PartEntryViewSet,
    LoanViewSet, AmanaViewSet,
)
router.register('overview', OverviewViewSet, basename='owner-overview')
router.register('parts', OwnerPartViewSet, basename='owner-parts')
router.register('boxes', OwnerBoxViewSet, basename='owner-boxes')
router.register('moves', OwnerMoveViewSet, basename='owner-moves')
router.register('lines', OwnerLineViewSet, basename='owner-lines')
router.register('entries', PartEntryViewSet, basename='owner-entries')
router.register('loans', LoanViewSet, basename='owner-loans')
router.register('amanat', AmanaViewSet, basename='owner-amanat')

# ── part B (partner_views) ──
from .partner_views import PartnerViewSet, SettlementViewSet, PartPnlViewSet  # noqa: E402
router.register('partners', PartnerViewSet, basename='owner-partners')
router.register('settlements', SettlementViewSet, basename='owner-settlements')
router.register('pnl', PartPnlViewSet, basename='owner-pnl')

urlpatterns = router.urls
