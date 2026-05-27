from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .api_admin import AdminStoreViewSet, AdminBranchViewSet, AdminUserViewSet

router = DefaultRouter()
router.register(r'stores',   AdminStoreViewSet,  basename='admin-store')
router.register(r'branches', AdminBranchViewSet, basename='admin-branch')
router.register(r'users',    AdminUserViewSet,   basename='admin-user')

urlpatterns = [
    path('', include(router.urls)),
]
