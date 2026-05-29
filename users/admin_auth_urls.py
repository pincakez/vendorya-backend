from django.urls import path
from .admin_auth import (
    AdminUserSearchView, AdminUserDetailView, AdminRenewPasswordView,
    AdminDisable2FAView, AdminClear2FATokensView,
)

urlpatterns = [
    path('users/', AdminUserSearchView.as_view(), name='admin-auth-user-search'),
    path('users/<int:user_id>/', AdminUserDetailView.as_view(), name='admin-auth-user-detail'),
    path('users/<int:user_id>/renew-password/', AdminRenewPasswordView.as_view(), name='admin-auth-renew-password'),
    path('users/<int:user_id>/disable-2fa/', AdminDisable2FAView.as_view(), name='admin-auth-disable-2fa'),
    path('users/<int:user_id>/clear-2fa-tokens/', AdminClear2FATokensView.as_view(), name='admin-auth-clear-2fa'),
]
