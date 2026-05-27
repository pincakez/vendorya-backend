"""Two URL modules in one file is ambiguous for Django's `include`, so we
expose `admin_urlpatterns` / `tenant_urlpatterns` via two thin shims
(`admin_urls.py` and `tenant_urls.py`) the project includes individually."""
