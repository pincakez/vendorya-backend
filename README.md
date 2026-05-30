<p align="center">
  <img src="logo-text.png" alt="Vendorya" width="320" />
</p>

<h1 align="center">Vendorya</h1>

<p align="center">
  <strong>A multi-tenant retail ERP & Point-of-Sale platform — inventory, finance, people, reporting, and an AI co-admin, in one system.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Django-6.0-092E20?logo=django&logoColor=white" alt="Django 6" />
  <img src="https://img.shields.io/badge/DRF-API-A30000?logo=django&logoColor=white" alt="DRF" />
  <img src="https://img.shields.io/badge/Vue-3-42b883?logo=vue.js&logoColor=white" alt="Vue 3" />
  <img src="https://img.shields.io/badge/Vite-build-646CFF?logo=vite&logoColor=white" alt="Vite" />
  <img src="https://img.shields.io/badge/Tailwind-v4-38BDF8?logo=tailwindcss&logoColor=white" alt="Tailwind v4" />
  <img src="https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/pgvector-RAG-4169E1?logo=postgresql&logoColor=white" alt="pgvector" />
  <img src="https://img.shields.io/badge/Gemini-AI%20admin-8E75FF?logo=googlegemini&logoColor=white" alt="Gemini" />
</p>

<p align="center"><em>This is the development monorepo. It ships to two GitHub repositories via <code>git subtree</code>: <a href="https://github.com/pincakez/vendorya-backend">vendorya-backend</a> &amp; <a href="https://github.com/pincakez/vendorya-frontend">vendorya-frontend</a>.</em></p>

<p align="center">
  <a href="https://vendorya.gatesinnov.com"><strong>Live Demo →</strong></a>
</p>

---

## ✦ What is Vendorya?

Vendorya is a **shared-database, multi-tenant SaaS** that runs an entire retail business — from purchasing stock to selling it at the till, tracking money, managing staff and customers, and producing accountant-grade reports. Every store ("tenant") lives in one database, isolated by a `store` foreign key that is enforced in **every** queryset and backed by a tenant-scoping middleware.

It is built for real retail accounting: **soft deletes everywhere**, **atomic stock & invoice sequencing**, **COGS traced to actual purchase prices**, and a **P&L that must reconcile to the cent** — if the numbers don't add up, it's treated as a bug, not a rounding issue.

> **Design philosophy:** the architect thinks in business logic and ERP workflows; the code stays faithful to accounting truth. No shortcuts on money.

---

## ✦ Highlights

| Area | What you get |
|---|---|
| 🎨 **UI** | White + orange store theme · zinc dark mode · collapsible group-card sidebar · split login · global ⌘K search |
| 🏬 **Multi-tenant** | Shared DB · `store` FK on every model · enforced in ViewSets **and** `TenantContextMiddleware` · UUID primary keys |
| 📦 **Inventory** | Products, dynamic per-store attributes, variants, SKUs (supplier-prefixed), multi-branch stock levels, categories, suppliers, taxes, stock adjustments |
| 💵 **Finance** | Sales invoices (draft → posted → void), partial payments, purchases (draft → received), expenses, work shifts + cash drawer, returns/refunds with restock |
| 👥 **People** | Customers (balance ledger), suppliers (computed balance), staff (role-based) |
| 📊 **Reports v1** | Sales, profit margin, A/R & A/P aging, reconciling P&L, expenses, stock ledger, cashier performance, tax — all exportable to **CSV / Excel / PDF** |
| 🔐 **Auth & security** | JWT (rotating refresh), role-scoped permissions, account lockout, login throttle, optional TOTP 2FA, IP allowlist, idle-logout |
| 🤖 **AI co-admin** | A server-side Gemini assistant with **48 function-calling tools**, pgvector RAG knowledge base, and a streaming chat panel inside the admin console |
| 💳 **Billing** | Subscription plans, tenant subscriptions, billing invoices (quota enforcement + processor integration on the roadmap) |
| 🔔 **Notifications** | In-app inbox (dispatcher + email templates on the roadmap) |
| 🌍 **Localization** | Per-store currency, timezone, decimal & separator rules; `dir=auto` Arabic-ready |

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
        │                    Django 6 + DRF (port 8000)                │
        │                                                              │
        │   TenantContextMiddleware  →  request.user.store (thread-local)
        │   RoleScopedPermission     →  per-action CASHIER→OWNER gates  │
        │                                                              │
        │   core · users · inventory · finance · reports               │
        │   billing · notifications · admin_ai · localization          │
        └───────────────┬───────────────────────────┬──────────────────┘
                        │                           │
                        ▼                           ▼
              ┌──────────────────┐         ┌────────────────────┐
              │  PostgreSQL 16   │         │   Gemini API        │
              │  + pgvector HNSW │         │  (AI admin tools)   │
              └──────────────────┘         └────────────────────┘
```

**Monorepo, two deploy targets.** Local development lives in one repo (`~/vendorya/`); it ships to GitHub as two repositories via `git subtree`:

```
~/vendorya/
├── vendorya-backend/     → github.com/pincakez/vendorya-backend   (Django 6 + DRF + PostgreSQL)
├── vendorya-frontend/    → github.com/pincakez/vendorya-frontend  (Vue 3 + Vite + Tailwind v4)
├── backups/              DB snapshots (10 max, backup_db.sh)
├── STATUS.md             current state
├── TODO.md               ordered roadmap with model hints
└── COMPLETED.md          archive of finished work
```

In production the Django app serves the built Vue bundle from `vendorya-frontend/dist/`, behind a permanent Cloudflare named tunnel.

---

## ✦ Tech Stack

**Backend** — Django 6 · Django REST Framework · SimpleJWT (rotating, blacklist) · PostgreSQL 16 · `pgvector` (HNSW cosine) · `django-axes` · `django-otp` (TOTP) · `google-genai` (Gemini).

**Frontend** — Vue 3 (Composition API) · Vite · Pinia · Vue Router 4 · Tailwind CSS v4 · Lucide icons · Axios · date-fns · `xlsx` / `jspdf` / `jspdf-autotable` (report exports).

---

## ✦ Multi-Tenancy & Security Model

- **Tenant isolation** — every tenant model carries a `store` FK. ViewSets filter `store=request.user.store` explicitly; `TenantContextMiddleware` stashes the active store in a thread-local and `TenantScopedManager` is the defense-in-depth backstop.
- **Roles** — `CASHIER < MANAGER < ADMIN < OWNER < SUPERADMIN`. Each ViewSet declares a `role_map` of the minimum role per action via `RoleScopedPermission` (fails closed on unknown actions).
- **Sudo / platform admin** — `is_superadmin` users have no store and act on a specific tenant by sending an `X-Store-ID` header.
- **Auth hardening** — rotating JWTs (access 1d, refresh 60d) with blacklist-after-rotation and an httpOnly refresh cookie; account lockout (5 fails / username+IP), login throttle, strong password validators, **opt-in** TOTP 2FA with backup codes, OWNER/ADMIN IP allowlist, and frontend idle-logout.
- **Data safety** — soft deletes only (`is_deleted=True`, never hard delete); stock moves only through PurchaseInvoice (in) / SalesInvoice (out) / StockAdjustment (correction); stock updates and invoice numbering wrapped in `transaction.atomic()`.

---

## ✦ Core Data Model (the parts that matter for money)

| Entity | Notes |
|---|---|
| `SalesInvoice` / `SalesInvoiceItem` | Status DRAFT→POSTED→VOID. Item carries `cost_at_sale` — a **COGS snapshot** taken at POST time from the weighted-average of *received* purchase prices. Outstanding = `grand_total − paid_amount`. |
| `Payment` | Partial payments supported; aggregates into `invoice.paid_amount`; `created_by` ties cash to a cashier. |
| `PurchaseInvoice` / `PurchaseItem` | Status DRAFT→RECEIVED. `unit_cost` is the true source of COGS; receiving adds stock atomically. |
| `RefundInvoice` / `RefundItem` | Per-item restock toggle; `created_by` attributes returns to a cashier; links to `original_invoice` for A/R netting. |
| `Expense` / `ExpenseCategory` | Hierarchical categories, dated amounts. |
| `WorkShift` | Open/close with `expected_cash`, `closing_cash`, and a calculated `difference` (over/short). |
| `Customer` (stored balance) / `Supplier` (computed balance) | |
| `ProductVariant` / `StockLevel` | Per-branch quantities; `cost_price` = last purchase price (display only — **reports never use it for COGS**). |
| `Tax` | Per-store rates, applied per line and summed per invoice. |

---

## ✦ Reports v1

Nine read-only, store-scoped endpoints under `/api/reports/` (Manager+ only), each with a matching Vue view and one-click **CSV / Excel / PDF** export:

| Report | What it answers |
|---|---|
| **Sales** | Revenue & quantity by **product / category / supplier / period** (day·week·month) |
| **Profit Margin** | Margin per product / category — COGS from the `cost_at_sale` snapshot, never an estimate |
| **A/R Aging** | Customer receivables in 0-30 / 31-60 / 61-90 / 90+ buckets — **net of partial payments and returns** (refunds linked via `original_invoice`) |
| **A/P Aging** | Supplier payables in the same buckets |
| **P&L** | Daily/weekly/monthly — `revenue − COGS − expenses − returns = net`, with a reconciliation guard |
| **Expenses** | Breakdown by category and by period |
| **Stock Ledger** | Per-variant audit trail (purchases / sales / adjustments / returns) with opening + running balance |
| **Cashier Performance** | Per staff: sales count & value, returns, and shift cash differences |
| **Tax** | Collected (output) tax by rate and period |

Export is centralized in `composables/useExport.js` + `components/ui/ExportMenu.vue` and rendered through a single reusable `ReportTable.vue` — built once, wired everywhere. All money is formatted via `utils/format.js`, never inline.

---

## ✦ AI Co-Admin

The admin console embeds a **Gemini-powered assistant** that can actually operate the platform:

- **48 function-calling tools** (22 read / 26 write) — create stores, products, customers, purchases, sales, expenses; query stats and activity logs; all per-store writes auto-scoped to the acting tenant.
- **RAG knowledge base** — upload PDFs/DOCX/CSV/TXT, embedded into `pgvector` (HNSW cosine), retrieved into prompts.
- **Streaming chat panel** in the admin layout — SSE token streaming, inline tool-call pills, slash-command palette, multimodal attachments.
- Server-side only: the API key never reaches the browser.

---

## ✦ Project Structure

```
vendorya-backend/
├── core/            Store, Branch, StoreSettings, ActivityLog, tenancy, middleware
├── users/           User (roles), Customer, JWT auth, 2FA, permissions
├── inventory/       Products, Variants, StockLevels, Categories, Suppliers, Attributes, Taxes
├── finance/         SalesInvoice, Payment, PurchaseInvoice, Expense, WorkShift, RefundInvoice
├── reports/         9 read-only reporting endpoints (Reports v1)
├── billing/         SubscriptionPlan, Subscription, BillingInvoice
├── notifications/   In-app inbox
├── admin_ai/        Gemini service, tool registry, pgvector RAG, chat SSE
└── vendorya_project/ settings + root urls

vendorya-frontend/
├── src/
│   ├── views/        page components (dashboard, inventory, finance, people, reports, admin, settings)
│   ├── components/   layout (sidebar/header) + ui (AppModal, AppPagination, QAB, ExportMenu, ReportFilters, ReportTable)
│   ├── composables/  useExport, useIdleTimeout, ...
│   ├── stores/       Pinia (auth, format, qab)
│   ├── utils/        format.js (fmtNum/fmtQty), date.js
│   ├── api/          axios instance (JWT + X-Store-ID + refresh rotation)
│   └── router/       routes + auth/role/store guards
└── public/           logo, icons, favicon
```

---

## ✦ Getting Started

### Prerequisites
- Python 3.12+ and PostgreSQL 16 (with the `pgvector` extension available)
- Node.js 20+

### 1 — Backend
```bash
cd vendorya-backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# configure your database + secrets (see settings.py / your .env)
python manage.py migrate
python manage.py createsuperuser     # creates a platform superadmin
python manage.py runserver           # http://localhost:8000
```

### 2 — Frontend
```bash
cd vendorya-frontend
npm install
npm run dev                          # http://localhost:5173
# or build for production:
npm run build                        # outputs to dist/ (served by Django in prod)
```

### 3 — Database
```bash
sudo service postgresql start
# create the database + a user, enable pgvector:
#   CREATE EXTENSION IF NOT EXISTS vector;
```

> Configure database name, user, and secrets through environment / settings — **never commit real credentials**.

---

## ✦ API Surface (top level)

| Prefix | Purpose |
|---|---|
| `/api/auth/` | Login, refresh, logout, 2FA, `/me`, customers, staff |
| `/api/core/` | Store, branches, settings, activity log, dashboard |
| `/api/inventory/` | Products, variants, categories, suppliers, attributes, taxes, adjustments |
| `/api/finance/` | Invoices, payments, purchases, expenses, shifts, returns |
| `/api/reports/` | The 9 Reports v1 endpoints |
| `/api/billing/` | Tenant subscription + invoices |
| `/api/notifications/` | Inbox |
| `/api/admin/…` | Sudo-only: stores, users, auth settings, billing, AI |

---

## ✦ Conventions

- **Backend** — every queryset filters by `store=request.user.store`; soft deletes only; stock & sequencing in `transaction.atomic()`; COGS from actual purchase prices.
- **Frontend** — Composition API + Pinia; numbers/currency always via `utils/format.js`; reuse `AppModal` / `AppPagination` / `QAB` / `ReportTable` rather than reinventing; QAB actions set in `onMounted`, cleared in `onUnmounted`.
- **Commits** — small and frequent; database is snapshotted before every migration (`backups/backup_db.sh`, keeps 10).

---

## ✦ Roadmap (abridged)

- Product add/edit form (API complete — frontend form not yet built)
- POS terminal (cart, barcode, receipt printing) + idempotent invoice posting
- Billing nightly jobs + quota enforcement + payment processor
- Notification dispatcher + transactional emails
- ETA / ZATCA e-invoicing, full RTL, bilingual invoices
- PWA install support

See [`TODO.md`](TODO.md) for the full, ordered backlog and [`COMPLETED.md`](COMPLETED.md) for the archive.

---

<p align="center"><sub>Vendorya — built with accounting truth in mind. © 2026.</sub></p>
