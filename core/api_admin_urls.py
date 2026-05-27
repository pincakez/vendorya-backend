from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .api_admin import (
    AdminStoreViewSet, AdminBranchViewSet, AdminUserViewSet,
    AdminActivityLogViewSet, AdminActivityLogMetaView,
)

router = DefaultRouter()
router.register(r'stores',         AdminStoreViewSet,        basename='admin-store')
router.register(r'branches',       AdminBranchViewSet,       basename='admin-branch')
router.register(r'users',          AdminUserViewSet,         basename='admin-user')
router.register(r'activity-logs',  AdminActivityLogViewSet,  basename='admin-activity-log')

urlpatterns = [
    path('activity-logs/meta/', AdminActivityLogMetaView.as_view(), name='admin-activity-log-meta'),
    path('', include(router.urls)),
]
