# Vendorya API Endpoints Reference

> ⚠️ **REWRITTEN AT THE TOP 2026-10-05 — the old list (kept unchanged at the bottom as history) was the very first
> Phase-1 list and badly out of date:** it showed 15 routes, an admin-panel POS at `/admin/core/store/…/pos/` that no
> longer exists, and Django's admin at `/admin/` (it is `/django-admin/` now). The real API has **217 distinct `/api/`
> routes**. The list below was generated from `manage.py show_urls` on the dev box (workshop, `vendorya_db_dev`,
> 2026-10-05); format-suffix variants (`.json`) are left out and `<pk>` is written `{id}`. Root URL conf:
> `vendorya_project/urls.py`, which `include()`s each app's `urls.py`. — Adam (Gcademy's Claude) for Yakot, 2026-10-05

## How the API works (2026-10-05)
- **Login:** `POST /api/auth/token/` (`username`, `password`) → access token in the body + the refresh token in an
  **httpOnly cookie**. `POST /api/auth/token/refresh/` reads the cookie (or a `refresh` field). Access 1 day, refresh 60 days, rotated + blacklisted.
- **Every other call:** `Authorization: Bearer <access>` (or a store API key: `Authorization: Api-Key vdy_…` / `X-API-Key: vdy_…`,
  scoped `<group>:read|write` over inventory · sales · purchasing · people · finance · reports). Everything is locked to the caller's store.
- **Roles:** each ViewSet declares a `role_map` (CASHIER → MANAGER → ADMIN → OWNER); `/api/admin/…` is sudo (platform super-admin) only.
- **Throttling:** login 5/min · API key 120/min (+ per-key limits). No global pagination default.
- **Public:** `GET /api/health/` only.

| Prefix | Routes | What lives there |
|---|---|---|
| `/api/auth/` | 18 | login, refresh, logout, `me`, change-password, 2FA, customers, staff, apply-pos-settings |
| `/api/core/` | 23 | store, logo, settings, branches, currencies, dashboard + widgets, label presets, activity logs, lock screen, nav search, QZ Tray signing |
| `/api/inventory/` | 51 | products (+ media, bulk actions, Memory Base import/dedup), variants, categories, attributes, suppliers, taxes, adjustments, transfers, storage, catalog CSV import/export |
| `/api/finance/` | 28 | sales invoices (checkout/void/print), payments, payment methods, refunds, purchases (receive/print/labels), supplier payments, shifts, expenses |
| `/api/reports/` | 15 | sales, profit margin, P&L, A/R + A/P aging, expenses, stock ledger, cashier performance, tax, expiry (+scan), 4 storage reports |
| `/api/pos/` | 6 | favorites (+reorder), top-selling |
| `/api/services/` | 8 | service / repair desk tickets + done/return/cancel/archive/toggle-bell, run-notifications |
| `/api/notifications/` | 8 | inbox, recent, unread count, read / read-all, preferences, admin sound |
| `/api/billing/` | 5 | the shop's own platform subscription + bills |
| `/api/smart/` | 7 | saved table layouts (presets, effective, my-config, assignments) — not AI |
| `/api/api-keys/` | 6 | the shop's API keys (+revoke), scopes |
| `/api/admin/` | 41 | sudo only: stores, branches, users, auth settings, activity logs, trash, isolation check, commands, billing (plans/subscriptions/invoices/settings), alerts |
| `/api/health/` | 1 | health check (public) |
| `/django-admin/` | — | Django's own admin (Jazzmin, OTP-guarded) — staff only |

## Every route, by prefix (from `show_urls`, dev, 2026-10-05)

### `/api/auth/` (18)
```
/api/auth/
/api/auth/2fa/backup-codes/regenerate/
/api/auth/2fa/disable/
/api/auth/2fa/setup/
/api/auth/2fa/status/
/api/auth/2fa/verify-setup/
/api/auth/apply-pos-settings/
/api/auth/change-password/
/api/auth/customers/
/api/auth/customers/{id}/
/api/auth/customers/{id}/invoices/
/api/auth/logout/
/api/auth/me/
/api/auth/staff/
/api/auth/staff/{id}/
/api/auth/token/
/api/auth/token/refresh/
/api/auth/{id}/
```

### `/api/core/` (23)
```
/api/core/
/api/core/branches/
/api/core/branches/{id}/
/api/core/branches/{id}/detail-data/
/api/core/currencies/
/api/core/currencies/{id}/
/api/core/dashboard-widgets/
/api/core/dashboard/
/api/core/label-presets/
/api/core/label-presets/{id}/
/api/core/lockscreen/facts/
/api/core/lockscreen/logo/
/api/core/lockscreen/pin/
/api/core/logs/
/api/core/logs/meta/
/api/core/logs/{id}/
/api/core/nav-search/
/api/core/qztray/cert/
/api/core/qztray/sign/
/api/core/settings/
/api/core/store/
/api/core/store/logo/
/api/core/{id}/
```

### `/api/inventory/` (51)
```
/api/inventory/
/api/inventory/adjustments/
/api/inventory/adjustments/{id}/
/api/inventory/attribute-definitions/
/api/inventory/attribute-definitions/{id}/
/api/inventory/attribute-definitions/{id}/add-option/
/api/inventory/attributes/
/api/inventory/attributes/{id}/
/api/inventory/attributes/{id}/add-option/
/api/inventory/catalog/export/
/api/inventory/catalog/import/commit/
/api/inventory/catalog/import/validate/
/api/inventory/categories/
/api/inventory/categories/{id}/
/api/inventory/categories/{id}/contents/
/api/inventory/categories/{id}/resolve-delete/
/api/inventory/products/
/api/inventory/products/alternatives/
/api/inventory/products/autocomplete/
/api/inventory/products/bulk_delete/
/api/inventory/products/bulk_ghost/
/api/inventory/products/bulk_update/
/api/inventory/products/dedup-memory-base/
/api/inventory/products/import-memory-base/
/api/inventory/products/media-specs/
/api/inventory/products/{id}/
/api/inventory/products/{id}/media/{id}/
/api/inventory/products/{id}/remove-image/
/api/inventory/products/{id}/reorder-media/
/api/inventory/products/{id}/showcase-settings/
/api/inventory/products/{id}/toggle_ghost/
/api/inventory/products/{id}/upload-image/
/api/inventory/products/{id}/upload-media/
/api/inventory/storage-locations/
/api/inventory/storage-locations/{id}/
/api/inventory/storage-locations/{id}/contents/
/api/inventory/storage/move-to/
/api/inventory/storage/movements/
/api/inventory/storage/retrieve/
/api/inventory/storage/write-off/
/api/inventory/suppliers/
/api/inventory/suppliers/check-prefix/
/api/inventory/suppliers/{id}/
/api/inventory/suppliers/{id}/purchases/
/api/inventory/taxes/
/api/inventory/taxes/{id}/
/api/inventory/transfers/
/api/inventory/transfers/{id}/
/api/inventory/variants/
/api/inventory/variants/{id}/
/api/inventory/{id}/
```

### `/api/finance/` (28)
```
/api/finance/
/api/finance/expense-categories/
/api/finance/expense-categories/{id}/
/api/finance/expenses/
/api/finance/expenses/{id}/
/api/finance/invoices/
/api/finance/invoices/{id}/
/api/finance/invoices/{id}/checkout/
/api/finance/invoices/{id}/print-data/
/api/finance/invoices/{id}/void/
/api/finance/payment-methods/
/api/finance/payment-methods/{id}/
/api/finance/payments/
/api/finance/payments/{id}/
/api/finance/purchases/
/api/finance/purchases/{id}/
/api/finance/purchases/{id}/label-data/
/api/finance/purchases/{id}/print-data/
/api/finance/purchases/{id}/receive/
/api/finance/refunds/
/api/finance/refunds/{id}/
/api/finance/shifts/
/api/finance/shifts/{id}/
/api/finance/shifts/{id}/close/
/api/finance/shifts/{id}/summary/
/api/finance/supplier-payments/
/api/finance/supplier-payments/{id}/
/api/finance/{id}/
```

### `/api/reports/` (15)
```
/api/reports/ap-aging/
/api/reports/ar-aging/
/api/reports/cashier-performance/
/api/reports/expenses/
/api/reports/expiry/
/api/reports/expiry/scan/
/api/reports/pnl/
/api/reports/profit-margin/
/api/reports/sales/
/api/reports/stock-ledger/
/api/reports/storage-aging/
/api/reports/storage-movements/
/api/reports/storage-reconciliation/
/api/reports/storage-value/
/api/reports/tax/
```

### `/api/pos/` (6)
```
/api/pos/
/api/pos/favorites/
/api/pos/favorites/reorder/
/api/pos/favorites/{id}/
/api/pos/top-selling/
/api/pos/{id}/
```

### `/api/services/` (8)
```
/api/services/
/api/services/run-notifications/
/api/services/{id}/
/api/services/{id}/archive/
/api/services/{id}/cancel/
/api/services/{id}/done/
/api/services/{id}/return/
/api/services/{id}/toggle-bell/
```

### `/api/notifications/` (8)
```
/api/notifications/
/api/notifications/admin-sound/
/api/notifications/preferences/
/api/notifications/read-all/
/api/notifications/recent/
/api/notifications/unread-count/
/api/notifications/{id}/
/api/notifications/{id}/read/
```

### `/api/billing/` (5)
```
/api/billing/
/api/billing/invoices/
/api/billing/invoices/{id}/
/api/billing/subscription/
/api/billing/{id}/
```

### `/api/smart/` (7)
```
/api/smart/
/api/smart/presets/
/api/smart/presets/assignments/
/api/smart/presets/effective/
/api/smart/presets/my-config/
/api/smart/presets/{id}/
/api/smart/{id}/
```

### `/api/api-keys/` (6)
```
/api/api-keys/
/api/api-keys/keys/
/api/api-keys/keys/{id}/
/api/api-keys/keys/{id}/revoke/
/api/api-keys/scopes/
/api/api-keys/{id}/
```

### `/api/admin/` (41)
```
/api/admin/
/api/admin/activity-logs/
/api/admin/activity-logs/meta/
/api/admin/activity-logs/purge/
/api/admin/activity-logs/{id}/
/api/admin/alerts/history/
/api/admin/alerts/send/
/api/admin/auth/users/
/api/admin/auth/users/{id}/
/api/admin/auth/users/{id}/clear-2fa-tokens/
/api/admin/auth/users/{id}/disable-2fa/
/api/admin/auth/users/{id}/renew-password/
/api/admin/billing/
/api/admin/billing/invoices/
/api/admin/billing/invoices/{id}/
/api/admin/billing/invoices/{id}/issue/
/api/admin/billing/invoices/{id}/mark-paid/
/api/admin/billing/invoices/{id}/void/
/api/admin/billing/plans/
/api/admin/billing/plans/{id}/
/api/admin/billing/settings/
/api/admin/billing/settings/run-cycle/
/api/admin/billing/subscriptions/
/api/admin/billing/subscriptions/{id}/
/api/admin/billing/{id}/
/api/admin/branches/
/api/admin/branches/{id}/
/api/admin/commands/sessions/
/api/admin/commands/user-logout/
/api/admin/isolation-check/
/api/admin/stores/
/api/admin/stores/check-code/
/api/admin/stores/{id}/
/api/admin/stores/{id}/export/
/api/admin/stores/{id}/force-logout/
/api/admin/stores/{id}/usage/
/api/admin/trash/
/api/admin/trash/restore/
/api/admin/users/
/api/admin/users/{id}/
/api/admin/{id}/
```

### `/api/health/` (1)
```
/api/health/
```

---
---

## 🗄️ The ORIGINAL list (Phase 1, 2026) — kept as history, do not build from it
> Sections 1–2 are still partly right (the routes exist; refresh is now cookie-based, attributes are also at
> `/api/inventory/attribute-definitions/`). **Sections 3 and 4 are DEAD:** there is no admin-panel POS or global search
> under `/admin/core/store/…` any more — the POS is the Vue app (`/api/pos/`, `/api/finance/invoices/…/checkout/`),
> search is `/api/core/nav-search/` + Typesense, and Django's admin moved to `/django-admin/`. — Adam (Gcademy's Claude) for Yakot, 2026-10-05


This document provides a comprehensive list of all API endpoints found in the Vendorya Django backend.

## 1. Authentication APIs
Used for user login and session management using JWT (JSON Web Tokens).

| URL Path | Method | Purpose | Parameters (Body/Query) |
| :--- | :--- | :--- | :--- |
| `/api/auth/token/` | `POST` | **Login**: Obtain Access and Refresh tokens. | `username`, `password` |
| `/api/auth/token/refresh/` | `POST` | **Refresh**: Get a new access token using a refresh token. | `refresh` (the refresh token string) |

---

## 2. Inventory Management APIs (REST Framework)
These endpoints are part of the main Inventory module. All require a `Bearer <token>` in the Authorization header. Actions are automatically isolated by the user's `store`.

### Products
| URL Path | Method | Purpose | Parameters |
| :--- | :--- | :--- | :--- |
| `/api/inventory/products/` | `GET` | List all products with summary (stock, price ranges). | **Query**: `search`, `ordering` |
| `/api/inventory/products/` | `POST` | Create a new product with variants. | **Body**: Product/Variant data |
| `/api/inventory/products/{id}/` | `GET` | Get full details of a specific product. | **Path**: `id` |
| `/api/inventory/products/{id}/` | `PUT` | Update a product (Full replace). | **Path**: `id`, **Body**: Full data |
| `/api/inventory/products/{id}/` | `PATCH` | Partially update a product. | **Path**: `id`, **Body**: Fields to update |
| `/api/inventory/products/{id}/` | `DELETE` | Soft-delete a product. | **Path**: `id` |

### Categories
| URL Path | Method | Purpose | Parameters |
| :--- | :--- | :--- | :--- |
| `/api/inventory/categories/` | `GET` | List all categories. | - |
| `/api/inventory/categories/` | `POST` | Create a category. | **Body**: `name`, `parent` |
| `/api/inventory/categories/{id}/` | `GET/PUT/PATCH/DELETE` | Standard CRUD operations for categories. | **Path**: `id` |

### Suppliers
| URL Path | Method | Purpose | Parameters |
| :--- | :--- | :--- | :--- |
| `/api/inventory/suppliers/` | `GET` | List all suppliers. | - |
| `/api/inventory/suppliers/` | `POST` | Register a new supplier. | **Body**: `name`, `contact_info`, `code_prefix` |
| `/api/inventory/suppliers/{id}/` | `GET/PUT/PATCH/DELETE` | Standard CRUD operations for suppliers. | **Path**: `id` |

### Attribute Definitions (Dynamic Columns)
| URL Path | Method | Purpose | Parameters |
| :--- | :--- | :--- | :--- |
| `/api/inventory/attributes/` | `GET` | List dynamic column definitions (e.g., Color, Size). | - |
| `/api/inventory/attributes/` | `POST` | Create a new attribute definition. | **Body**: `name`, `key`, `input_type` |
| `/api/inventory/attributes/{id}/` | `GET/PUT/PATCH/DELETE` | Standard CRUD operations for definitions. | **Path**: `id` |

---

## 3. Internal Admin APIs
These endpoints are used within the Django Admin custom views (e.g., POS and Global Search). They require an active admin session or `login_required`.

| URL Path | Method | Purpose | Parameters |
| :--- | :--- | :--- | :--- |
| `/admin/core/store/{store_id}/search/api/` | `GET` | **Global Search**: Search across Products, Suppliers, Categories, and Customers. | **Query**: `q` (min 3 chars) |
| `/admin/core/store/{store_id}/pos/api/search/` | `GET` | **POS Search**: Specifically searches Product Variants with stock levels for the POS. | **Query**: `q` |
| `/admin/core/store/{store_id}/pos/api/checkout/` | `POST` | **POS Checkout**: Finalizes a sale. Creates an invoice, deducts stock, and records payment. | **Body**: `{ "items": [ { "id": "v_id", "qty": 1 }, ... ] }` |

---

## 4. Admin Management (UI)
The following are page-rendering endpoints (HTML) used in the admin interface:

| URL Path | Method | Purpose |
| :--- | :--- | :--- |
| `/admin/` | `GET` | Main Admin Dashboard. |
| `/admin/core/store/{store_id}/search/` | `GET` | Global search interface page. |
| `/admin/core/store/{store_id}/pos/` | `GET` | POS (Point of Sale) terminal interface. |

---

> [!NOTE]
> Endpoints under `/api/inventory/` use the Django Rest Framework and follow standard RESTful conventions unless otherwise specified.
