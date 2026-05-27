from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ProductViewSet, CategoryViewSet, SupplierViewSet, AttributeDefinitionViewSet

router = DefaultRouter()
router.register(r'products', ProductViewSet, basename='product')
router.register(r'categories', CategoryViewSet, basename='category')
router.register(r'suppliers', SupplierViewSet, basename='supplier')
router.register(r'attributes', AttributeDefinitionViewSet, basename='attribute-definition')

urlpatterns = [
    path('', include(router.urls)),
]