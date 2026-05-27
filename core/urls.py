from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import StoreView, StoreSettingsView, BranchViewSet, ActivityLogViewSet

router = DefaultRouter()
router.register(r'branches', BranchViewSet,      basename='branch')
router.register(r'logs',     ActivityLogViewSet, basename='activity-log')

urlpatterns = [
    path('store/',    StoreView.as_view(),         name='store'),
    path('settings/', StoreSettingsView.as_view(), name='store-settings'),
    path('', include(router.urls)),
]
