from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    VendoryaTokenObtainView, CookieTokenRefreshView, LogoutView,
    MeView, ChangePasswordView, CustomerViewSet, StaffViewSet,
)
from .views_2fa import (
    TwoFactorSetupView, TwoFactorVerifySetupView, TwoFactorStatusView,
    TwoFactorDisableView, TwoFactorBackupRegenerateView,
)

router = DefaultRouter()
router.register('customers', CustomerViewSet, basename='customers')
router.register('staff', StaffViewSet, basename='staff')

urlpatterns = [
    path('token/', VendoryaTokenObtainView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', CookieTokenRefreshView.as_view(), name='token_refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('me/', MeView.as_view(), name='me'),
    path('change-password/', ChangePasswordView.as_view(), name='change_password'),

    # Two-factor (TOTP)
    path('2fa/setup/', TwoFactorSetupView.as_view(), name='2fa_setup'),
    path('2fa/verify-setup/', TwoFactorVerifySetupView.as_view(), name='2fa_verify_setup'),
    path('2fa/status/', TwoFactorStatusView.as_view(), name='2fa_status'),
    path('2fa/disable/', TwoFactorDisableView.as_view(), name='2fa_disable'),
    path('2fa/backup-codes/regenerate/', TwoFactorBackupRegenerateView.as_view(), name='2fa_backup_regen'),

    path('', include(router.urls)),
]
