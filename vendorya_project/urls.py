import os
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include, re_path
from django.http import FileResponse, Http404

def serve_vue(request, path=''):
    index = os.path.join(settings.BASE_DIR, '..', 'vendorya-frontend', 'dist', 'index.html')
    if os.path.exists(index):
        return FileResponse(open(index, 'rb'), content_type='text/html')
    raise Http404

urlpatterns = [
    path('django-admin/', admin.site.urls),

    # API URLs
    path('api/core/',      include('core.urls')),
    path('api/inventory/', include('inventory.urls')),
    path('api/finance/',   include('finance.urls')),

    # Super-admin API (sudo-only)
    path('api/admin/',         include('core.api_admin_urls')),
    path('api/admin/billing/', include('billing.admin_urls')),
    path('api/admin/ai/',      include('admin_ai.urls')),

    # Tenant billing + notifications
    path('api/billing/',       include('billing.tenant_urls')),
    path('api/notifications/', include('notifications.urls')),

    # Auth URLs (login, refresh, logout, 2FA, me, customers, staff)
    path('api/auth/', include('users.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Serve Vue assets (js/css from dist/assets/)
vue_assets = os.path.join(settings.BASE_DIR, '..', 'vendorya-frontend', 'dist', 'assets')
if os.path.exists(vue_assets):
    urlpatterns += static('/assets/', document_root=vue_assets)

# Catch-all: serve Vue index.html for any non-API route (SPA routing)
urlpatterns += [re_path(r'^(?!api/|django-admin/|static/|media/|assets/).*$', serve_vue)]