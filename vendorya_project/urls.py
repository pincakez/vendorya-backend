import os
import mimetypes
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include, re_path
from django.http import FileResponse, Http404

def serve_vue(request, path=''):
    index = os.path.join(settings.BASE_DIR, '..', 'vendorya-frontend', 'dist', 'index.html')
    if os.path.exists(index):
        response = FileResponse(open(index, 'rb'), content_type='text/html')
        # Never let Cloudflare or browsers cache index.html — hashed JS/CSS are fine to cache
        response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response['Pragma'] = 'no-cache'
        return response
    raise Http404

urlpatterns = [
    path('django-admin/', admin.site.urls),

    # API URLs
    path('api/core/',      include('core.urls')),
    path('api/inventory/', include('inventory.urls')),
    path('api/finance/',   include('finance.urls')),
    path('api/reports/',   include('reports.urls')),

    # Super-admin API (sudo-only)
    path('api/admin/',         include('core.api_admin_urls')),
    path('api/admin/auth/',    include('users.admin_auth_urls')),
    path('api/admin/billing/', include('billing.admin_urls')),
    path('api/admin/ai/',      include('admin_ai.urls')),
    path('api/admin/alerts/',  include('notifications.admin_urls')),

    # Tenant billing + notifications
    path('api/billing/',       include('billing.tenant_urls')),
    path('api/notifications/', include('notifications.urls')),
    path('api/smart/',         include('smart_analysis.urls')),

    # Auth URLs (login, refresh, logout, 2FA, me, customers, staff)
    path('api/auth/', include('users.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

def serve_dist_file(request, filename):
    """Serve root-level static files from dist/ (logos, favicon, manifest,
    PWA service worker, etc.)"""
    dist_file = os.path.join(settings.BASE_DIR, '..', 'vendorya-frontend', 'dist', filename)
    if os.path.exists(dist_file) and os.path.isfile(dist_file):
        content_type, _ = mimetypes.guess_type(dist_file)
        # Force a JS content-type for the service worker / registration files —
        # mimetypes can return text/plain on some systems, which the browser
        # rejects for SW registration.
        if filename.endswith(('.js', '.mjs')):
            content_type = 'text/javascript'
        resp = FileResponse(open(dist_file, 'rb'), content_type=content_type or 'application/octet-stream')
        # The SW + manifest must never be served stale (Cloudflare/browser),
        # or updates won't roll out. Allow root scope for the worker.
        if filename in ('sw.js', 'registerSW.js') or filename.endswith('.webmanifest'):
            resp['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        if filename == 'sw.js':
            resp['Service-Worker-Allowed'] = '/'
        return resp
    raise Http404

# Serve Vue assets (js/css from dist/assets/)
vue_assets = os.path.join(settings.BASE_DIR, '..', 'vendorya-frontend', 'dist', 'assets')
if os.path.exists(vue_assets):
    urlpatterns += static('/assets/', document_root=vue_assets)

# Serve root-level dist files (logos, favicon, manifest, robots.txt, etc.)
# Must be before the SPA catch-all
urlpatterns += [
    re_path(r'^(?P<filename>[\w.-]+\.(png|jpg|jpeg|gif|svg|ico|webp|woff2?|ttf|json|txt|xml|webmanifest|js|mjs))$',
            serve_dist_file)
]

# Catch-all: serve Vue index.html for any non-API route (SPA routing)
urlpatterns += [re_path(r'^(?!api/|django-admin/|static/|media/|assets/).*$', serve_vue)]