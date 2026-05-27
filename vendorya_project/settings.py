"""
Django settings for vendorya_project project.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from the backend root (next to manage.py).  Does nothing in prod
# if the file is absent — env vars set by the deploy take over.
load_dotenv(BASE_DIR / '.env')


def _env_bool(key, default=False):
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _env_list(key, default=None):
    raw = os.environ.get(key, '')
    items = [item.strip() for item in raw.split(',') if item.strip()]
    return items or (default or [])


def _env_required(key):
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"Required environment variable {key!r} is not set. "
            f"Copy .env.example to .env or export it in your deploy."
        )
    return value


# Core security knobs — must be supplied via env (no in-repo fallback).
SECRET_KEY = _env_required('DJANGO_SECRET_KEY')
DEBUG = _env_bool('DJANGO_DEBUG', default=False)
ALLOWED_HOSTS = _env_list('DJANGO_ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])


# Application definition

INSTALLED_APPS = [
    'jazzmin',  # Must be before admin
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework_simplejwt',
    'django_extensions',
    

    # Third Party
    'rest_framework',
    'corsheaders',

    # Local Apps
    'import_export',
    'core',
    'users',
    'inventory',
    'finance',
    'smart_analysis',
    'billing',
    'notifications',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'core.middleware.TenantContextMiddleware',
]

ROOT_URLCONF = 'vendorya_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'vendorya_project.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE':   'django.db.backends.postgresql',
        'NAME':     _env_required('DB_NAME'),
        'USER':     _env_required('DB_USER'),
        'PASSWORD': _env_required('DB_PASSWORD'),
        'HOST':     os.environ.get('DB_HOST', 'localhost'),
        'PORT':     os.environ.get('DB_PORT', '5432'),
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Africa/Cairo'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'users.User'

# CORS: allow-all is *only* permitted in DEBUG. In prod, set CORS_ALLOWED_ORIGINS
# explicitly via .env (comma-separated list of full origins).
_cors_origins = _env_list('CORS_ALLOWED_ORIGINS')
if DEBUG and not _cors_origins:
    CORS_ALLOW_ALL_ORIGINS = True
else:
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGINS = _cors_origins

import os
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# JWT Settings
from datetime import timedelta

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'users.authentication.VendoryaJWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    )
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': _env_required('JWT_SIGNING_KEY'),
    'AUDIENCE': None,
    'ISSUER': None,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
}

JAZZMIN_SETTINGS = {
    # UI Customizer
    "site_title": "Vendorya ERP",
    "site_header": "Vendorya",
    "site_brand": "Vendorya",
    "welcome_sign": "Welcome to Vendorya ERP",
    "copyright": "Vendorya Ltd",
    "search_model": "users.Customer",

    # LOGOS
    "site_logo": "img/logo.png",
    "login_logo": "img/logo.png",
    "site_logo_classes": "img-circle",

    # Top Menu
    "topmenu_links": [
        {"name": "Dashboard", "url": "admin:index", "permissions": ["auth.view_user"]},
        {"name": "POS (Coming Soon)", "url": "#", "new_window": True},
        {"model": "users.User"},
    ],

    # User Menu
    "usermenu_links": [
        {"name": "Support", "url": "#", "new_window": True},
        {"model": "users.User"}
    ],

    # Side Menu
    "show_sidebar": True,
    "navigation_expanded": True,
    "hide_apps": [],
    "hide_models": ["inventory.ProductAttribute", "inventory.BundleItem"],

    # Icons
    "icons": {
        "auth": "fas fa-users-cog",
        "auth.user": "fas fa-user",
        "auth.Group": "fas fa-users",
        
        # Core
        "core.Store": "fas fa-store",
        "core.Branch": "fas fa-building",
        "core.Address": "fas fa-map-marker-alt",
        "core.ActivityLog": "fas fa-history",
        "core.StoreSettings": "fas fa-cogs",
        
        # Inventory
        "inventory.Product": "fas fa-tshirt",
        "inventory.ProductVariant": "fas fa-tags",
        "inventory.StockLevel": "fas fa-boxes",
        "inventory.StockAdjustment": "fas fa-wrench",
        "inventory.Category": "fas fa-sitemap",
        "inventory.Supplier": "fas fa-truck",
        "inventory.Tax": "fas fa-percent",
        "inventory.AttributeDefinition": "fas fa-list",
        
        # Finance
        "finance.WorkShift": "fas fa-clock",
        "finance.SalesInvoice": "fas fa-file-invoice-dollar",
        "finance.PurchaseInvoice": "fas fa-truck-loading", # NEW
        "finance.RefundInvoice": "fas fa-undo-alt",
        "finance.Payment": "fas fa-money-bill-wave",
        "finance.Expense": "fas fa-receipt",
        "finance.ExpenseCategory": "fas fa-folder-open",
        "finance.PaymentMethod": "fas fa-credit-card",
        "finance.InvoiceSequence": "fas fa-sort-numeric-up",
        
        # Users
        "users.Customer": "fas fa-user-tie",
        "users.User": "fas fa-user-shield",
    },

    # Sidebar Order
    "order_with_respect_to": [
        # Operations
        "finance.WorkShift",
        "finance.SalesInvoice",
        "finance.PurchaseInvoice", # NEW
        "finance.RefundInvoice",
        "finance.Payment",
        "finance.Expense",
        
        # Inventory
        "inventory.Product",
        "inventory.ProductVariant",
        "inventory.StockLevel",
        "inventory.StockAdjustment",
        
        # CRM
        "users.Customer",
        "inventory.Supplier",
        
        # Configuration
        "core.Store",
        "core.StoreSettings",
        "core.Branch",
        "inventory.Category",
        "inventory.Tax",
        "finance.PaymentMethod",
        
        # System
        "core.ActivityLog",
        "auth.User",
        "auth.Group",
    ],

    # UI Tweaks
    "related_modal_active": True,
    "custom_css": "css/admin_fix.css",
    "show_ui_builder": False,
}
X_FRAME_OPTIONS = 'SAMEORIGIN'
