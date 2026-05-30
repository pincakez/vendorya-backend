from rest_framework.routers import DefaultRouter
from .views import TablePresetViewSet

router = DefaultRouter()
router.register('presets', TablePresetViewSet, basename='table-preset')

urlpatterns = router.urls
