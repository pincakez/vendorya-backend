# Vendorya API Endpoints Reference
> **Regenerated 2026-10-14 (s167) from `manage.py show_urls` on dev — 214 distinct `/api/` routes** (format-suffix variants left out,
> `<pk>`/`<int:…>` written `{id}`). Root URL conf `vendorya_project/urls.py`, which `include()`s each app's `urls.py`.
> To refresh: `venv/bin/python manage.py show_urls | grep '^/api' | awk '{print $1}' | grep -vi format | sed -E 's/<[^>]+>/{id}/g' | sort -u`.
> New in s167: customer receipts, customer + supplier statements, products label-data. (The original Phase-1 list was removed — git history.)

## How the API works (checked 2026-10-14)
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
| `/api/core/` | 22 | store, logo, settings, branches, currencies, dashboard + widgets, label presets, activity logs, lock screen, nav search, QZ Tray signing |
| `/api/inventory/` | 52 | products (+ media, bulk actions, Memory Base import/dedup), variants, categories, attributes, suppliers, taxes, adjustments, transfers, storage, catalog CSV import/export |
| `/api/finance/` | 30 | sales invoices (checkout with split payment / void / print), payments, customer receipts (debt payments), payment methods, refunds, purchases (receive/print/labels), supplier payments, shifts + drawer, expenses |
| `/api/reports/` | 15 | sales, profit margin, P&L, A/R + A/P aging, expenses, stock ledger, cashier performance, tax, expiry (+scan), 4 storage reports |
| `/api/pos/` | 5 | favorites (+reorder), top-selling |
| `/api/services/` | 8 | service / repair desk tickets + done/return/cancel/archive/toggle-bell, run-notifications |
| `/api/notifications/` | 8 | inbox, recent, unread count, read / read-all, preferences, admin sound |
| `/api/billing/` | 4 | the shop's own platform subscription + bills |
| `/api/smart/` | 6 | saved table layouts (presets, effective, my-config, assignments) — not AI |
| `/api/api-keys/` | 5 | the shop's API keys (+revoke), scopes |
| `/api/admin/` | 40 | sudo only: stores, branches, users, auth settings, activity logs, trash, isolation check, commands, billing (plans/subscriptions/invoices/settings), alerts |
| `/api/health/` | 1 | health check (public) |
| `/django-admin/` | — | Django's own admin (Jazzmin, OTP-guarded) — staff only |

## Every route, by prefix (from `show_urls`, dev, 2026-10-14)

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
/api/auth/customers/{id}/statement/
/api/auth/logout/
/api/auth/me/
/api/auth/staff/
/api/auth/staff/{id}/
/api/auth/token/
/api/auth/token/refresh/
```

### `/api/core/` (22)
```
/api/core/
/api/core/branches/
/api/core/branches/{id}/
/api/core/branches/{id}/detail-data/
/api/core/currencies/
/api/core/currencies/{id}/
/api/core/dashboard/
/api/core/dashboard-widgets/
/api/core/label-presets/
/api/core/label-presets/{id}/
/api/core/lockscreen/facts/
/api/core/lockscreen/logo/
/api/core/lockscreen/pin/
/api/core/logs/
/api/core/logs/{id}/
/api/core/logs/meta/
/api/core/nav-search/
/api/core/qztray/cert/
/api/core/qztray/sign/
/api/core/settings/
/api/core/store/
/api/core/store/logo/
```

### `/api/inventory/` (52)
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
/api/inventory/products/{id}/
/api/inventory/products/{id}/media/{id}/
/api/inventory/products/{id}/remove-image/
/api/inventory/products/{id}/reorder-media/
/api/inventory/products/{id}/showcase-settings/
/api/inventory/products/{id}/toggle_ghost/
/api/inventory/products/{id}/upload-image/
/api/inventory/products/{id}/upload-media/
/api/inventory/products/import-memory-base/
/api/inventory/products/label-data/
/api/inventory/products/media-specs/
/api/inventory/storage-locations/
/api/inventory/storage-locations/{id}/
/api/inventory/storage-locations/{id}/contents/
/api/inventory/storage/movements/
/api/inventory/storage/move-to/
/api/inventory/storage/retrieve/
/api/inventory/storage/write-off/
/api/inventory/suppliers/
/api/inventory/suppliers/check-prefix/
/api/inventory/suppliers/{id}/
/api/inventory/suppliers/{id}/purchases/
/api/inventory/suppliers/{id}/statement/
/api/inventory/taxes/
/api/inventory/taxes/{id}/
/api/inventory/transfers/
/api/inventory/transfers/{id}/
/api/inventory/variants/
/api/inventory/variants/{id}/
```

### `/api/finance/` (30)
```
/api/finance/
/api/finance/customer-receipts/
/api/finance/customer-receipts/{id}/
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
/api/finance/shifts/drawer/
/api/finance/shifts/{id}/
/api/finance/shifts/{id}/close/
/api/finance/shifts/{id}/summary/
/api/finance/supplier-payments/
/api/finance/supplier-payments/{id}/
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

### `/api/pos/` (5)
```
/api/pos/
/api/pos/favorites/
/api/pos/favorites/{id}/
/api/pos/favorites/reorder/
/api/pos/top-selling/
```

### `/api/services/` (8)
```
/api/services/
/api/services/{id}/
/api/services/{id}/archive/
/api/services/{id}/cancel/
/api/services/{id}/done/
/api/services/{id}/return/
/api/services/{id}/toggle-bell/
/api/services/run-notifications/
```

### `/api/notifications/` (8)
```
/api/notifications/
/api/notifications/admin-sound/
/api/notifications/{id}/
/api/notifications/{id}/read/
/api/notifications/preferences/
/api/notifications/read-all/
/api/notifications/recent/
/api/notifications/unread-count/
```

### `/api/billing/` (4)
```
/api/billing/
/api/billing/invoices/
/api/billing/invoices/{id}/
/api/billing/subscription/
```

### `/api/smart/` (6)
```
/api/smart/
/api/smart/presets/
/api/smart/presets/assignments/
/api/smart/presets/effective/
/api/smart/presets/{id}/
/api/smart/presets/my-config/
```

### `/api/api-keys/` (5)
```
/api/api-keys/
/api/api-keys/keys/
/api/api-keys/keys/{id}/
/api/api-keys/keys/{id}/revoke/
/api/api-keys/scopes/
```

### `/api/admin/` (40)
```
/api/admin/
/api/admin/activity-logs/
/api/admin/activity-logs/{id}/
/api/admin/activity-logs/meta/
/api/admin/activity-logs/purge/
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
/api/admin/branches/
/api/admin/branches/{id}/
/api/admin/commands/sessions/
/api/admin/commands/user-logout/
/api/admin/isolation-check/
/api/admin/stores/
/api/admin/stores/check-code/
/api/admin/stores/{id}/
/api/admin/stores/{id}/force-logout/
/api/admin/stores/{id}/sku/
/api/admin/stores/{id}/sku2/disable/
/api/admin/stores/{id}/usage/
/api/admin/trash/
/api/admin/trash/restore/
/api/admin/users/
/api/admin/users/{id}/
```

### `/api/health/` (1)
```
/api/health/
```
