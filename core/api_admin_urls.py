from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .api_admin import (
    AdminStoreViewSet, AdminBranchViewSet, AdminUserViewSet,
    AdminActivityLogViewSet, AdminActivityLogMetaView, AdminActivityLogPurgeView,
    AdminStoreCodeCheckView, AdminStoreForceLogoutView,
    AdminStoreUsageView, AdminStoreExportView,
    AdminSessionsView, AdminUserForceLogoutView,
)
from .api_admin_trash import AdminTrashListView, AdminTrashRestoreView
from .api_admin_isolation import AdminIsolationAuditView

router = DefaultRouter()
router.register(r'stores',         AdminStoreViewSet,        basename='admin-store')
router.register(r'branches',       AdminBranchViewSet,       basename='admin-branch')
router.register(r'users',          AdminUserViewSet,         basename='admin-user')
router.register(r'activity-logs',  AdminActivityLogViewSet,  basename='admin-activity-log')

urlpatterns = [
    path('activity-logs/meta/', AdminActivityLogMetaView.as_view(), name='admin-activity-log-meta'),
    path('activity-logs/purge/', AdminActivityLogPurgeView.as_view(), name='admin-activity-log-purge'),
    path('stores/check-code/',  AdminStoreCodeCheckView.as_view(),  name='admin-store-check-code'),
    path('stores/<uuid:store_id>/force-logout/', AdminStoreForceLogoutView.as_view(), name='admin-store-force-logout'),
    path('stores/<uuid:store_id>/usage/',        AdminStoreUsageView.as_view(),       name='admin-store-usage'),
    path('stores/<uuid:store_id>/export/',       AdminStoreExportView.as_view(),       name='admin-store-export'),
    path('trash/',         AdminTrashListView.as_view(),    name='admin-trash-list'),
    path('trash/restore/', AdminTrashRestoreView.as_view(), name='admin-trash-restore'),
    path('isolation-check/', AdminIsolationAuditView.as_view(), name='admin-isolation-check'),
    path('commands/sessions/',    AdminSessionsView.as_view(),        name='admin-commands-sessions'),
    path('commands/user-logout/', AdminUserForceLogoutView.as_view(), name='admin-commands-user-logout'),
    path('', include(router.urls)),
]
