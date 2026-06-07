<p align="center">
  <img src="logo-text.png" alt="Vendorya" width="320" />
</p>

<h1 align="center">Vendorya</h1>

<p align="center">
  <strong>A multi-tenant retail ERP & Point-of-Sale platform — inventory, finance, POS, reporting, and an AI co-admin, in one system.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Django-6.0-092E20?logo=django&logoColor=white" alt="Django 6" />
  <img src="https://img.shields.io/badge/DRF-API-A30000?logo=django&logoColor=white" alt="DRF" />
  <img src="https://img.shields.io/badge/Vue-3-42b883?logo=vue.js&logoColor=white" alt="Vue 3" />
  <img src="https://img.shields.io/badge/Vite-build-646CFF?logo=vite&logoColor=white" alt="Vite" />
  <img src="https://img.shields.io/badge/Tailwind-v4-38BDF8?logo=tailwindcss&logoColor=white" alt="Tailwind v4" />
  <img src="https://img.shields.io/badge/PostgreSQL-18-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/pgvector-RAG-4169E1?logo=postgresql&logoColor=white" alt="pgvector" />
  <img src="https://img.shields.io/badge/Gemini-AI%20admin-8E75FF?logo=googlegemini&logoColor=white" alt="Gemini" />
</p>

<p align="center">
  <a href="https://vendorya.gatesinnov.com"><strong>Live Demo →</strong></a>
</p>

---

## ✦ What is Vendorya?

Vendorya is a **shared-database, multi-tenant SaaS** that runs an entire retail business — from purchasing stock to selling it at the till, tracking money, managing staff and customers, and producing accountant-grade reports. Every store ("tenant") lives in one database, isolated by a `store` foreign key enforced in **every** queryset and backed by a tenant-scoping middleware.

It is built for real retail accounting: **soft deletes everywhere**, **atomic stock & invoice sequencing**, **COGS traced to actual purchase prices**, and a **P&L that must reconcile to the cent** — if the numbers don't add up, it's a bug.

> **Design philosophy:** the architect thinks in business logic and ERP workflows; the code stays faithful to accounting truth. No shortcuts on money.

---

## ✦ Highlights

| Area | What you get |
|---|---|
| 🎨 **UI** | White + orange store theme · zinc dark mode · Roboto font · collapsible group-card sidebar (260px) · Barlow-style nav · split login with animated background |
| 🏬 **Multi-tenant** | Shared DB · `store` FK on every model · enforced in ViewSets **and** `TenantContextMiddleware` + `TenantScopedManager` · UUID primary keys · automated isolation audit (23/23 endpoints verified) |
| 📦 **Inventory** | Products, dynamic per-store attributes, variants, 10-digit supplier-prefixed SKUs, multi-branch stock levels, 4-tier categories (tree UI), suppliers, taxes, stock adjustments, bulk ghost/hide/edit/delete, CSV import+export |
| 🖥️ **POS** | Full terminal: product search, top-selling panel, favorites, cart, per-item & header discounts, hold/resume, Agel (credit) selling, cash-change calc, receipt overlay, keyboard shortcuts, idempotent checkout |
| 💵 **Finance** | Sales invoices (draft→posted→void) with per-line discount, partial payments, purchases (draft→received) with barcode label printing, expenses, work shifts + cash drawer, returns/refunds with restock |
| 👥 **People** | Customers (live AR via `customer_outstanding()`), suppliers (computed balance), staff (role-based) |
| 📊 **Reports** | 9 endpoints: sales, profit margin, A/R & A/P aging, reconciling P&L, expenses, stock ledger, cashier performance, tax — all exportable to **CSV / Excel / PDF** |
| 🔐 **Auth & security** | JWT (rotating refresh, httpOnly cookie), role-scoped permissions, account lockout, login throttle, optional TOTP 2FA, IP allowlist, idle-logout, 64-byte signing key |
| 🤖 **AI co-admin** | Gemini assistant with **48 function-calling tools**, pgvector RAG knowledge base, streaming chat panel, agentic tool-loop, two-warning protocol for destructive actions |
| 🏷️ **Label printing** | Configurable `LabelPreset` model (width/height/fields), bulk print from purchase receipts via JsBarcode SVG + CSS `@page` sizing |
| 💳 **Billing** | Subscription plans, tenant subscriptions, quota enforcement, billing invoices, manual run-cycle button |
| 🔔 **Notifications** | Priority-based in-app inbox with dispatcher, alerts center, user preferences + sounds |
| 🌍 **Localization** | Per-store currency, timezone, decimal & separator rules; `dir=auto` Arabic-ready |
| 📱 **PWA** | Service worker, installable, full icon set (favicon · apple-touch · maskable) |

---

## ✦ Architecture

```
                         ┌──────────────────────────────┐
                         │        Browser / PWA          │
                         │  Vue 3 · Pinia · Vue Router    │
                         └───────────────┬────────────────┘
                                         │  Axios (JWT + X-Store-ID)
                                         ▼
        ┌────────────────────────────────────────────────────────────┐
        │                    Django 6 + DRF (port 8000)              │
        │                                                            │
        │  TenantContextMiddleware  →  request.user.store            │
        │  RoleScopedPermission     →  per-action CASHIER→OWNER gate │
        │                                                            │
        │  core · users · inventory · finance · pos · reports        │
        │  billing · notifications · admin_ai · localization         │
        └───────────────┬───────────────────────────┬────────────────┘
                        │                           │
                        ▼                           ▼
              ┌──────────────────┐         ┌────────────────────┐
              │  PostgreSQL 18   │         │   Gemini API        │
              │  + pgvector HNSW │         │  (AI admin tools)   │
              └──────────────────┘         └────────────────────┘
```

**Monorepo, two deploy targets.** Local development lives in `~/vendorya/`; ships to GitHub as two repositories via `git subtree`:

```
~/vendorya/
├── vendorya-backend/     → github.com/pincakez/vendorya-backend   (Django 6 + DRF)
├── vendorya-frontend/    → github.com/pincakez/vendorya-frontend  (Vue 3 + Vite)
├── backups/              DB snapshots (10 max, auto-committed)
├── STATUS.md             live state — read every session
├── TODO.md               open work · parked ideas · verification debt · changelog
└── DEPLOY.md             production deployment reference
```

In production, Django serves the built Vue bundle from `vendorya-frontend/dist/` on AWS EC2, behind Cloudflare (Full SSL mode).

---

## ✦ Tech Stack

**Backend** — Django 6 · Django REST Framework · SimpleJWT (rotating refresh, httpOnly cookie) · PostgreSQL 18 · `pgvector` (HNSW cosine) · `django-axes` · `django-otp` (TOTP 2FA) · `google-genai` (Gemini SDK).

**Frontend** — Vue 3 (Composition API) · Vite · Pinia · Vue Router 4 · Tailwind CSS v4 · Lucide icons · Axios · date-fns · JsBarcode · `xlsx` / `jspdf` / `jspdf-autotable` (report exports) · `vite-plugin-pwa`.

---

## ✦ Multi-Tenancy & Security Model

- **Tenant isolation** — every tenant model carries a `store` FK. ViewSets explicitly filter `store=request.user.store`; `TenantContextMiddleware` stashes the active store in a thread-local; `TenantScopedManager` replaces the default manager on all core ERP models as a defense-in-depth backstop. Automated isolation audit: `manage.py check_isolation` probes all discovered endpoints — currently 23/23 pass.
- **Roles** — `CASHIER < MANAGER < ADMIN < OWNER < SUPERADMIN`. Each ViewSet declares a `role_map` per action via `RoleScopedPermission` (fails closed on unknown actions).
- **Sudo / platform admin** — `is_superadmin` users have no store and act on a specific tenant via `X-Store-ID` header.
- **Auth hardening** — rotating JWTs (access 1d, refresh 60d) with blacklist-after-rotation stored in an httpOnly cookie; access token is memory-only (never localStorage); account lockout (5 fails, username + IP), login throttle, full Django password validators, opt-in TOTP 2FA with backup codes, OWNER/ADMIN IP allowlist, frontend idle-logout, 64-byte HMAC signing key.
- **Data safety** — soft deletes only (`is_deleted=True`, never hard delete); stock moves only through `PurchaseInvoice` (in) / `SalesInvoice` (out) / `StockAdjustment` (correction); all stock and sequencing wrapped in `transaction.atomic()`; `select_for_update` on concurrent SKU generation and refund restock.

---

## ✦ Core Data Model

| Entity | Notes |
|---|---|
| `SalesInvoice` / `SalesInvoiceItem` | Status DRAFT→POSTED→VOID. Item carries `cost_at_sale` (COGS snapshot at POST time) and `discount_amount` (per-line discount). `grand_total = subtotal + tax − line_discounts − header_discount`. Outstanding = `grand_total − paid_amount`. |
| `Payment` | Partial payments; aggregates into `invoice.paid_amount`; `created_by` ties cash to a cashier. |
| `PurchaseInvoice` / `PurchaseItem` | Status DRAFT→RECEIVED. `unit_cost` is the true COGS source; receiving adds stock atomically. Supports bulk barcode label printing via `label-data` action. |
| `RefundInvoice` / `RefundItem` | Per-item restock toggle; validated against original invoice (qty cap, foreign-variant rejection); `select_for_update` on restock and numbering. |
| `Expense` / `ExpenseCategory` | Hierarchical categories, dated amounts. |
| `WorkShift` | Open/close with `expected_cash`, `closing_cash`, calculated `difference` (over/short). |
| `Customer` | Live AR via `customer_outstanding()` — sums `grand_total − paid_amount − refunded` across non-deleted invoices. Credit-limit policy: ALLOW / WARN / BLOCK. Walk-in customer auto-seeded per store. |
| `ProductVariant` / `StockLevel` | Per-branch quantities; `cost_price` = last purchase price (display only — COGS reports use `cost_at_sale`). |
| `Tax` | Per-store rates; server-side per-line calculation (`_apply_line_tax`); `tax_enabled` master switch. |
| `LabelPreset` | Per-store barcode label templates (width/height mm, field toggles). |
| `PaymentMethod` | Per-store; `is_cash` and `is_agel` flags; 5 defaults seeded on store creation. |

---

## ✦ Reports

Nine read-only, store-scoped endpoints under `/api/reports/` (Manager+ only), each with a matching Vue view and one-click **CSV / Excel / PDF** export:

| Report | What it answers |
|---|---|
| **Sales** | Revenue & quantity by product / category / supplier / period |
| **Profit Margin** | Margin per product / category — COGS from `cost_at_sale`, never an estimate |
| **A/R Aging** | Customer receivables in 0-30 / 31-60 / 61-90 / 90+ buckets, net of payments and returns |
| **A/P Aging** | Supplier payables in the same buckets |
| **P&L** | `revenue − COGS − expenses − returns = net` with reconciliation guard |
| **Expenses** | Breakdown by category and period |
| **Stock Ledger** | Per-variant audit trail with opening + running balance |
| **Cashier Performance** | Per staff: sales count & value, returns, shift cash differences |
| **Tax** | Collected (output) tax by rate and period |

---

## ✦ AI Co-Admin (V-Pilot)

The admin console embeds a **Gemini-powered assistant** that can actually operate the platform:

- **48 function-calling tools** (22 read / 26 write) — create stores, products, customers, purchases, sales, expenses; query stats and logs; all per-store writes auto-scoped to the acting tenant; all tool password flows run Django's full password validators.
- **RAG knowledge base** — upload PDFs/DOCX/CSV/TXT; embedded into `pgvector` HNSW; retrieved into prompts. 10 MB cap + extension allowlist.
- **Agentic tool loop** — model calls tool → results fed back → repeat (cap 8 hops); no stall on multi-hop queries.
- **Streaming chat panel** in the admin layout — SSE token streaming, inline tool-call chips, two-warning protocol before destructive/bulk actions.
- **Self-updating model list** — mirrors Google's `models.list()` on refresh; safe pin/hide overrides per store on the Misc page.
- Server-side only: the API key never reaches the browser.

---

## ✦ Project Structure

```
vendorya-backend/
├── core/            Store, Branch, StoreSettings, LabelPreset, ActivityLog, tenancy, middleware
├── users/           User (roles), Customer, JWT auth, 2FA, permissions
├── inventory/       Products, Variants, StockLevels, Categories (4-tier tree), Suppliers, Attributes, Taxes, Import/Export engine
├── finance/         SalesInvoice (per-line discount), Payment, PurchaseInvoice, Expense, WorkShift, RefundInvoice
├── pos/             POSFavoriteItem, top-selling endpoint, apply-pos-settings broadcast
├── reports/         9 read-only reporting endpoints
├── billing/         SubscriptionPlan, Subscription, BillingInvoice, quota enforcement
├── notifications/   Priority-based in-app inbox, dispatcher
├── admin_ai/        Gemini service, 48-tool registry, pgvector RAG, chat SSE, model cache
└── vendorya_project/ settings + root urls

vendorya-frontend/
├── src/
│   ├── views/
│   │   ├── Dashboard.vue, Login.vue, ActivityLog.vue
│   │   ├── inventory/    Products, Purchases, Adjustments, Categories, Attributes, ImportExport
│   │   ├── sales/        SalesInvoices, InvoicePrint, Returns
│   │   ├── finance/      Shifts, Expenses, CashDrawer
│   │   ├── people/       Customers, Suppliers, Staff
│   │   ├── reports/      9 report views + ReportFilters/ReportTable
│   │   ├── settings/     8-tab Settings page + Taxes, Profile, Security, Billing, Notifications, POS sub-pages
│   │   ├── admin/        Dashboard, Stores, Branches, Users, Activity, Plans, Subscriptions, AI, Misc, Alerts, Trash, IsolationCheck
│   │   ├── pos/          (POS.vue at views/POS.vue + PaymentModal, DiscountModal, BranchPickerModal)
│   │   └── print/        LabelPrint.vue
│   ├── components/
│   │   ├── layout/       AppSidebar, AppHeader, AppFooter, AdminLayout, POSLayout
│   │   └── ui/           AppModal, AppPagination, Money, PhysicalButton, ExportMenu, ReportTable, ReportFilters
│   ├── composables/      useExport, useIdleTimeout
│   ├── stores/           auth, format, qab, cart, pos, ui
│   ├── utils/            format.js (fmtNum/fmtQty), date.js (enUS locale, tz-aware)
│   ├── api/              axios (JWT memory-only + httpOnly refresh cookie + X-Store-ID)
│   └── router/           routes + auth/role/store guards
└── public/               logo, icons, favicon, PWA manifest
```

---

## ✦ Getting Started

### Prerequisites
- Python 3.12+ and PostgreSQL 16+ (with the `pgvector` extension)
- Node.js 20+

### 1 — Backend
```bash
cd vendorya-backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# copy and configure .env (DB credentials, JWT_SIGNING_KEY, etc.)
python manage.py migrate
python manage.py createsuperuser     # creates a platform superadmin
python manage.py runserver           # http://localhost:8000
```

### 2 — Frontend
```bash
cd vendorya-frontend
npm install
npm run dev          # http://localhost:5173 (Vite dev server)
npm run build        # outputs to dist/ (served by Django in prod)
```

### 3 — Database
```bash
sudo service postgresql start
# psql: CREATE EXTENSION IF NOT EXISTS vector;
```

> Configure database credentials through `.env` — **never commit real credentials**.

---

## ✦ API Surface

| Prefix | Purpose |
|---|---|
| `/api/auth/` | Login, refresh, logout, 2FA, `/me`, customers, staff |
| `/api/core/` | Store, branches, settings, label presets, activity log, dashboard |
| `/api/inventory/` | Products, variants, categories, suppliers, attributes, taxes, adjustments, import/export |
| `/api/finance/` | Invoices (with per-line discount), payments, purchases (+ label-data), expenses, shifts, returns |
| `/api/pos/` | Top-selling products, favorites, POS settings broadcast |
| `/api/reports/` | 9 Reports endpoints |
| `/api/billing/` | Tenant subscription + invoices |
| `/api/notifications/` | Inbox |
| `/api/admin/…` | Sudo-only: stores, users, auth settings, billing, AI, trash, isolation audit |

---

## ✦ Conventions

- **Backend** — every queryset filters `store=request.user.store`; soft deletes only; stock & sequencing in `transaction.atomic()`; COGS from actual purchase prices (`cost_at_sale`); server-side tax calculation (never trust client-supplied `tax_amount`).
- **Frontend** — Composition API + Pinia; numbers/currency always via `utils/format.js` (`fmtNum` / `fmtQty`) or the `<Money>` component — never inline; reuse `AppModal` / `AppPagination` / `ReportTable`; primary actions live in page headers (no floating action buttons); all layouts via `DefaultLayout` / `POSLayout` / `AdminLayout`.
- **Commits** — small and frequent; DB snapshotted before every migration (`backups/backup_db.sh`, keeps 10, auto-committed).

---

## ✦ Roadmap

- Customer / Supplier / Product detail pages (drill-down views)
- Returns: time-window enforcement, restocking fee, store-credit vs cash-refund
- Reorder levels per variant (currently a hardcoded threshold)
- Billing nightly scheduler (manual "Run cycle now" button exists; automation is opt-in)
- Transactional emails (SMTP / Postmark / SES — needs credentials)
- Egypt ETA e-invoicing / Saudi ZATCA (legal threshold research first)
- Full RTL CSS layout flip + bilingual invoices (AR + EN)
- Public API + per-store API key control center

See [`TODO.md`](TODO.md) for the full ordered backlog.

---

<p align="center"><sub>Vendorya — built with accounting truth in mind. © 2026.</sub></p>
