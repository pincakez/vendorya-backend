from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import TokenRefreshView

urlpatterns = [
    path('admin/', admin.site.urls),

    # API URLs
    path('api/core/',      include('core.urls')),
    path('api/inventory/', include('inventory.urls')),
    path('api/finance/',   include('finance.urls')),

    # Auth URLs
    path('api/auth/', include('users.urls')),
    path('api/auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)