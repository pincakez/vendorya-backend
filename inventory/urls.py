from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ProductViewSet, ProductVariantViewSet,
    CategoryViewSet, SupplierViewSet, AttributeDefinitionViewSet, TaxViewSet,
    StockAdjustmentViewSet, StockTransferViewSet, SupplierPrefixCheckView,
    CatalogImportValidateView, CatalogImportCommitView, CatalogExportView,
)

router = DefaultRouter()
router.register(r'products',    ProductViewSet,             basename='product')
router.register(r'variants',    ProductVariantViewSet,      basename='variant')
router.register(r'categories',  CategoryViewSet,            basename='category')
router.register(r'suppliers',   SupplierViewSet,            basename='supplier')
router.register(r'attributes',            AttributeDefinitionViewSet, basename='attribute-definition')
router.register(r'attribute-definitions', AttributeDefinitionViewSet, basename='attribute-definition-alias')
router.register(r'taxes',       TaxViewSet,                 basename='tax')
router.register(r'adjustments', StockAdjustmentViewSet,    basename='adjustment')
router.register(r'transfers',   StockTransferViewSet,      basename='transfer')

urlpatterns = [
    path('suppliers/check-prefix/', SupplierPrefixCheckView.as_view(), name='supplier-check-prefix'),
    path('catalog/import/validate/', CatalogImportValidateView.as_view(), name='catalog-import-validate'),
    path('catalog/import/commit/',   CatalogImportCommitView.as_view(),   name='catalog-import-commit'),
    path('catalog/export/',          CatalogExportView.as_view(),         name='catalog-export'),
    path('', include(router.urls)),
]
