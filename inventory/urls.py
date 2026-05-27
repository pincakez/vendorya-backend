from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ProductViewSet, ProductVariantViewSet,
    CategoryViewSet, SupplierViewSet, AttributeDefinitionViewSet, TaxViewSet,
)

router = DefaultRouter()
router.register(r'products',   ProductViewSet,             basename='product')
router.register(r'variants',   ProductVariantViewSet,      basename='variant')
router.register(r'categories', CategoryViewSet,            basename='category')
router.register(r'suppliers',  SupplierViewSet,            basename='supplier')
router.register(r'attributes', AttributeDefinitionViewSet, basename='attribute-definition')
router.register(r'taxes',      TaxViewSet,                 basename='tax')

urlpatterns = [
    path('', include(router.urls)),
]
