from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import POSFavoriteItemViewSet, TopSellingView

router = DefaultRouter()
router.register('favorites', POSFavoriteItemViewSet, basename='pos-favorites')

urlpatterns = [
    path('top-selling/', TopSellingView.as_view(), name='pos-top-selling'),
    path('', include(router.urls)),
]
