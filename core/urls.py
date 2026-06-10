from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    StoreView, StoreSettingsView, StoreLogoView, BranchViewSet, ActivityLogViewSet,
    ActivityLogMetaView, DashboardView, CurrencyViewSet, LabelPresetViewSet,
    QZTrayCertView, QZTraySignView,
)

router = DefaultRouter()
router.register(r'branches',      BranchViewSet,      basename='branch')
router.register(r'logs',          ActivityLogViewSet, basename='activity-log')
router.register(r'currencies',    CurrencyViewSet,    basename='currency')
router.register(r'label-presets', LabelPresetViewSet, basename='label-preset')

urlpatterns = [
    path('store/',         StoreView.as_view(),         name='store'),
    path('store/logo/',    StoreLogoView.as_view(),     name='store-logo'),
    path('settings/',      StoreSettingsView.as_view(), name='store-settings'),
    path('dashboard/',     DashboardView.as_view(),     name='dashboard'),
    path('logs/meta/',     ActivityLogMetaView.as_view(), name='activity-log-meta'),
    path('qztray/cert/',   QZTrayCertView.as_view(),     name='qztray-cert'),
    path('qztray/sign/',   QZTraySignView.as_view(),     name='qztray-sign'),
    path('', include(router.urls)),
]
