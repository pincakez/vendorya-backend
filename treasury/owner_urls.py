"""`/api/treasury/owner/` — the owner level (TREASURY.md § 12). Part A registers under "part A", part B under "part B"
(separate comment blocks → the two agents never edit the same lines)."""
from rest_framework.routers import DefaultRouter

router = DefaultRouter()

# ── part A (owner_views) ──

# ── part B (partner_views) ──
from .partner_views import PartnerViewSet, SettlementViewSet, PartPnlViewSet  # noqa: E402
router.register('partners', PartnerViewSet, basename='owner-partners')
router.register('settlements', SettlementViewSet, basename='owner-settlements')
router.register('pnl', PartPnlViewSet, basename='owner-pnl')

urlpatterns = router.urls
