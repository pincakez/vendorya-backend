from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    NotificationViewSet,
    NotificationPreferenceView,
    AdminAlertView,
    AdminAlertHistoryView,
)

router = DefaultRouter()
router.register(r'', NotificationViewSet, basename='notification')

urlpatterns = [
    path('preferences/', NotificationPreferenceView.as_view(), name='notification-prefs'),
    # Admin alert endpoints are mounted under /api/admin/ via core urls
] + router.urls
