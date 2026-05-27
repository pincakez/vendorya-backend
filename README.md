# Vendorya ERP

> A modern, headless, multi-tenant SaaS ERP system built for small and medium retail businesses.

![Status](https://img.shields.io/badge/Backend-Complete-brightgreen)
![Status](https://img.shields.io/badge/Frontend-In%20Development-yellow)
![Django](https://img.shields.io/badge/Django-6.x-092E20?logo=django)
![Vue](https://img.shields.io/badge/Vue-3-42b883?logo=vue.js)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791?logo=postgresql)
![License](https://img.shields.io/badge/License-Private-red)

---

## What is Vendorya?

Vendorya is a full-stack ERP system designed for retail store owners who need a professional, fast, and reliable tool to manage their business — from point of sale to purchasing, inventory, finance, and customer relationships.

Built as a **multi-tenant SaaS platform** — multiple independent store owners share the same infrastructure, each seeing only their own data, with complete isolation enforced at the API level.

---

## Features

### Backend (Complete)
- **Multi-tenant architecture** — shared schema, per-store data isolation
- **Inventory management** — products, variants, dynamic attributes, categories, suppliers
- **Stock control** — multi-branch stock levels, purchase invoices, stock adjustments
- **Finance module** — sales invoices, refunds, expenses, payment methods
- **Work shifts** — cashier shift open/close with cash shortage detection
- **Customer CRM** — customer profiles with credit/debt balance tracking
- **Role-based access** — Owner, Manager, Cashier
- **Soft deletes** — nothing is ever permanently deleted
- **Sequential invoicing** — human-readable invoice numbers per store (1001, 1002...)
- **Audit logs** — every action tracked with user, timestamp, and IP
- **Store settings** — per-store tax ID, receipt header/footer, policies

### Frontend (In Development)
- Vue 3 SPA with dark/light mode design system
- TanStack Table for all data grids
- Full POS terminal
- Dashboard with real-time stats

---

## Tech Stack

### Backend
| Technology | Version | Purpose |
|---|---|---|
| Python | 3.12 | Runtime |
| Django | 6.x | Web framework |
| Django REST Framework | 3.16 | API layer |
| SimpleJWT | 5.5 | Authentication |
| PostgreSQL | 16 | Database |
| Gunicorn | 23.x | Production server |
| Jazzmin | 3.x | Admin theme |

### Frontend
| Technology | Purpose |
|---|---|
| Vue 3 + Vite | UI framework |
| Pinia | State management |
| Tailwind CSS v4 | Styling |
| TanStack Table | Data grids |
| Headless UI | Accessible components |
| Lucide Icons | Icon library |
| Axios | HTTP client |

---

## Architecture

```
┌─────────────────────────────────┐
│     Vue 3 SPA (Port 5173)       │
│   Axios + JWT Bearer Token      │
└──────────────┬──────────────────┘
               │ REST API
┌──────────────▼──────────────────┐
│   Django Backend (Port 8000)    │
│   DRF + SimpleJWT               │
│                                 │
│  core │ inventory │ finance      │
│  users │ smart_analysis          │
└──────────────┬──────────────────┘
               │ psycopg2
┌──────────────▼──────────────────┐
│     PostgreSQL 16 (Port 5432)   │
└─────────────────────────────────┘
```

---

## Project Structure

```
vendorya-backend/
├── vendorya_project/     # Django settings and URL config
├── core/                 # Store, Branch, Address, ActivityLog, StoreSettings
├── users/                # Custom User model, Customer model
├── inventory/            # Products, Variants, Stock, Categories, Suppliers, Taxes
├── finance/              # Invoices, Payments, Purchases, Shifts, Refunds, Expenses
├── smart_analysis/       # Table preferences (AI analysis — planned)
└── static/               # Static assets for admin
```

---

## Data Architecture (Multi-Tenant)

Shared database, shared schema. Every model has a `store` FK. Tenant isolation enforced at the ViewSet level — every queryset must filter by `request.user.store`.

```
Store (Tenant)
 ├── Branch (physical locations)
 ├── User (staff: Owner / Admin / Manager / Cashier)
 ├── Customer (with balance/debt tracking)
 ├── Product → ProductVariant → StockLevel (per branch)
 ├── SalesInvoice → SalesInvoiceItem + Payment
 ├── PurchaseInvoice → PurchaseItem (adds stock on RECEIVED)
 ├── RefundInvoice → RefundItem
 ├── WorkShift (cashier sessions)
 └── Expense + ExpenseCategory
```

---

## Local Setup (WSL / Ubuntu)

**Prerequisites:** Python 3.12, PostgreSQL 16

```bash
cd ~/vendorya/vendorya-backend
source venv/bin/activate
pip install -r requirements.txt
sudo service postgresql start
python manage.py migrate
python manage.py runserver
```

Server: `http://localhost:8000` | Admin: `http://localhost:8000/admin/`

---

## API Endpoints

### Authentication
```
POST  /api/auth/token/          Login → access + refresh tokens
POST  /api/auth/token/refresh/  Refresh access token
```
All endpoints require `Authorization: Bearer <token>`.

### Inventory
```
GET/POST        /api/inventory/products/
GET/PUT/DELETE  /api/inventory/products/{id}/
GET/POST        /api/inventory/categories/
GET/POST        /api/inventory/suppliers/
GET/POST        /api/inventory/attributes/
```

> Finance, Users, and Core API endpoints pending implementation.

---

## Current API Status

| Module | Models | Admin | API |
|---|---|---|---|
| Core (Store / Branch) | ✅ | ✅ | ⏳ |
| Users | ✅ | ✅ | ⏳ |
| Inventory | ✅ | ✅ | ✅ |
| Finance | ✅ | ✅ | ⏳ |
| Smart Analysis | ✅ | — | ⏳ |

---

## Before Going to Production

- [ ] Move `SECRET_KEY`, DB credentials, JWT key to `.env`
- [ ] Set `DEBUG=False`, proper `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`
- [ ] Wrap invoice sequencing and stock updates in `transaction.atomic()`
- [ ] Implement global tenant middleware as safety net
