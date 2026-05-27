from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import VendoryaTokenObtainView, MeView, CustomerViewSet

router = DefaultRouter()
router.register('customers', CustomerViewSet, basename='customers')

urlpatterns = [
    path('token/', VendoryaTokenObtainView.as_view(), name='token_obtain_pair'),
    path('me/', MeView.as_view(), name='me'),
    path('', include(router.urls)),
]
