[VENDORYA_README.md](https://github.com/user-attachments/files/26077832/VENDORYA_README.md)
# Vendorya ERP

> A modern, headless, multi-tenant SaaS ERP system built for small and medium retail businesses.

![Status](https://img.shields.io/badge/Backend-Complete-brightgreen)
![Status](https://img.shields.io/badge/Frontend-In%20Development-yellow)
![Django](https://img.shields.io/badge/Django-5.2.8-092E20?logo=django)
![Vue](https://img.shields.io/badge/Vue-3-42b883?logo=vue.js)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791?logo=postgresql)
![License](https://img.shields.io/badge/License-Private-red)

---

## What is Vendorya?

Vendorya is a full-stack ERP (Enterprise Resource Planning) system designed for retail store owners who need a professional, fast, and reliable tool to manage their business — from the point of sale to purchasing, inventory, finance, and customer relationships.

It is built as a **multi-tenant SaaS platform** — multiple independent store owners share the same infrastructure, each seeing only their own data, with complete isolation enforced at the API level.

---

## Features

### ✅ Completed (Backend)
- **Multi-tenant architecture** — shared schema, per-store data isolation
- **Point of Sale (POS)** — shift-enforced checkout with atomic transactions
- **Inventory management** — products, variants, categories, suppliers, attributes
- **Stock control** — multi-branch stock levels, purchase invoices, stock adjustments
- **Finance module** — sales invoices, refunds, expenses, payment methods
- **Work shifts** — cashier shift open/close with cash shortage detection
- **Customer CRM** — customer profiles with credit/debt balance tracking (Agel system)
- **Role-based access** — Owner, Manager, Cashier with granular permissions
- **Soft deletes** — nothing is ever permanently deleted
- **Sequential invoicing** — human-readable invoice numbers per store (1001, 1002...)
- **Audit logs** — every action tracked with user, timestamp, and IP
- **Store settings** — per-store configuration (tax ID, receipt header/footer, policies)
- **Admin interface** — fully themed with Jazzmin, dark mode ready

### 🚧 In Progress (Frontend)
- Vue 3 SPA with dark mode design system
- Pixel-perfect UI built from Photoshop concepts
- TanStack Table for all data grids
- Full POS terminal (keyboard-first, Zen mode)
- Dashboard with real-time stats

---

## Tech Stack

### Backend
| Technology | Version | Purpose |
|---|---|---|
| Python | 3.12 | Runtime |
| Django | 5.2.8 | Web framework |
| Django REST Framework | 3.16.1 | API layer |
| SimpleJWT | 5.5.1 | Authentication |
| PostgreSQL | 16 | Database |
| Gunicorn | 23.0.0 | Production server |
| Jazzmin | 3.0.1 | Admin theme |

### Frontend
| Technology | Purpose |
|---|---|
| Vue 3 + Vite | UI framework and build tool |
| Pinia | State management |
| Tailwind CSS v4 | Styling |
| TanStack Table | Data grids |
| Headless UI | Accessible components |
| Lucide Icons | Icon library |
| Axios | HTTP client |
| Zod | Form validation |

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
vendorya/
├── vendorya-backend/
│   ├── core/               # Store, Branch, Address, Audit logs
│   ├── inventory/          # Products, Variants, Stock, Suppliers
│   ├── finance/            # Invoices, Shifts, Payments, Refunds
│   ├── users/              # Auth, Roles, Customers
│   ├── smart_analysis/     # User preferences, analytics
│   └── vendorya_project/   # Django settings and URL config
│
└── vendorya-frontend/
    └── src/                # Vue 3 SPA (in development)
```

---

## Getting Started

### Prerequisites

- Python 3.12+
- PostgreSQL 16
- Node.js 18+
- WSL2 (Ubuntu 24.04) — recommended for Windows users

### Backend Setup

```bash
# 1. Clone the repository
git clone https://github.com/pincakez/vendorya-backend.git
cd vendorya-backend

# 2. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env with your own credentials

# 5. Create the database
sudo -u postgres psql
CREATE DATABASE vendorya_db;
CREATE USER your_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE vendorya_db TO your_user;
\q

# 6. Run migrations
python manage.py migrate

# 7. Create a superuser
python manage.py createsuperuser

# 8. Create default roles
python create_roles.py

# 9. Start the server
python manage.py runserver
```

### Access

| Service | URL |
|---|---|
| Admin Panel | http://localhost:8000/admin/ |
| API Root | http://localhost:8000/api/ |
| Auth Endpoint | http://localhost:8000/api/auth/token/ |

---

## API Overview

### Authentication
```
POST /api/auth/token/         → Get access + refresh tokens
POST /api/auth/token/refresh/ → Refresh access token
```

All API requests require:
```
Authorization: Bearer <access_token>
```

### Inventory
```
GET|POST   /api/inventory/products/
GET|PUT|PATCH|DELETE /api/inventory/products/{id}/
GET|POST   /api/inventory/categories/
GET|POST   /api/inventory/suppliers/
GET|POST   /api/inventory/attributes/
```

### More endpoints coming as frontend development progresses.

---

## Business Rules

- **Cashiers cannot sell without an open shift** — API returns 403
- **Stock only moves through official channels** — PurchaseInvoice, SalesInvoice, or StockAdjustment
- **Nothing is ever deleted** — soft deletes only, data is always recoverable
- **Every store is isolated** — tenant filtering is enforced on every API endpoint
- **Invoice numbers are sequential per store** — human-readable (1001, 1002...)

---

## Roles & Permissions

| Role | Capabilities |
|---|---|
| **Owner** | Full access including store settings, staff management, audit logs |
| **Manager** | Inventory, purchases, stock adjustments, expenses |
| **Cashier** | POS only — open/close shift, process sales |

---

## Roadmap

- [x] Core multi-tenancy architecture
- [x] Full inventory system with variants and attributes
- [x] POS with shift enforcement and atomic checkout
- [x] Purchase invoices with automatic stock updates
- [x] Refund system
- [x] Customer credit/debt tracking (Agel system)
- [x] Role-based permissions
- [ ] Vue 3 frontend — Sidebar and layout
- [ ] Vue 3 frontend — Dashboard
- [ ] Vue 3 frontend — Full POS terminal
- [ ] Vue 3 frontend — Inventory management pages
- [ ] Vue 3 frontend — Finance pages
- [ ] DRF endpoints for finance and users modules
- [ ] Global tenant middleware
- [ ] Cloud deployment
- [ ] Test suite

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```env
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=*
DB_NAME=vendorya_db
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_HOST=localhost
DB_PORT=5432
JWT_SIGNING_KEY=your-jwt-signing-key
```

---

## Contributing

This is a private project. Not open for external contributions at this time.

---

## License

Private — All rights reserved.

---

*Built with Django, Vue 3, and a lot of persistence.*
