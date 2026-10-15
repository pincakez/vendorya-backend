from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    MoneyBoxViewSet, TransferViewSet, MoneyMoveViewSet, IncomeCategoryViewSet, OtherIncomeViewSet, TreasuryReportViewSet,
)

router = DefaultRouter()
router.register('boxes', MoneyBoxViewSet, basename='treasury-boxes')
router.register('transfers', TransferViewSet, basename='treasury-transfers')
router.register('moves', MoneyMoveViewSet, basename='treasury-moves')
router.register('income-categories', IncomeCategoryViewSet, basename='treasury-income-categories')
router.register('other-income', OtherIncomeViewSet, basename='treasury-other-income')
router.register('reports', TreasuryReportViewSet, basename='treasury-reports')

urlpatterns = [path('owner/', include('treasury.owner_urls'))] + router.urls
