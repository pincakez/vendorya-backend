from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import APIKeyViewSet, APIScopeCatalogView

router = DefaultRouter()
router.register(r'keys', APIKeyViewSet, basename='api-key')

urlpatterns = [
    path('scopes/', APIScopeCatalogView.as_view(), name='api-scope-catalog'),
    path('', include(router.urls)),
]
