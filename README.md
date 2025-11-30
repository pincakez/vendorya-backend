# Vendorya ERP - Backend Blueprint (Live Document)

This document outlines the architecture, database schema, and API structure for the Vendorya multi-tenant ERP system. It is the single source of truth for the project.

---

## 1. Core Architecture

- **Framework:** Django (Python)
- **API:** Django REST Framework (DRF)
- **Database:** PostgreSQL
- **Architecture:** Multi-Tenant SaaS (Software as a Service)
- **Primary Tenant Key:** `Store` model (`core.Store`)
- **Authentication:** JWT (JSON Web Tokens) via `djangorestframework-simplejwt`

---

## 2. Database Schema (Models)

### `core` app
- `Store`: The central tenant. All other data links to this.
- `Address`: Reusable address model for branches and customers.
- `Branch`: A physical location belonging to a `Store`.

### `users` app
- `User`: Custom user model inheriting from Django's `AbstractUser`. Linked to a `Store`. Has roles (`OWNER`, `MANAGER`, etc.).
- `Customer`: A customer belonging to a specific `Store`.

### `inventory` app
- `Supplier`: A supplier linked to a `Store`. Provides `code_prefix` for products.
- `Category`: Product categories, linked to a `Store`. Can be nested.
- `Product`: The central inventory item.
    - Contains standard fields (`name`, `price`, `stock_quantity`, `wholesale_price`).
    - **Key Feature:** `attributes` (JSONField). Allows for flexible, store-specific data like `{"Size": "M", "Color": "Red"}` for a clothing store or `{"Expiry": "2025-12-01"}` for a pharmacy.
    - **Automation:** `product_code` is auto-generated based on the supplier's prefix.

### `finance` app
- `PaymentMethod`: e.g., Cash, Visa. Linked to a `Store`.
- `SalesInvoice` & `SalesInvoiceItem`: Tracks sales transactions.
    - **Automation:** Invoice totals are calculated automatically via Django Signals whenever an item is added, changed, or removed.

### `smart_analysis` app
- `TablePreference`: Stores user-specific UI settings for data tables (e.g., visible columns, sort order). This is a JSONField to support the frontend's dynamic tables.

---

## 3. API Endpoints

All endpoints require a `Bearer` token in the `Authorization` header.

### Authentication (`/api/auth/`)
- **`POST /api/auth/token/`**:
  - **Body:** `{"username": "...", "password": "..."}`
  - **Returns:** JWT `access` and `refresh` tokens.
- **`POST /api/auth/token/refresh/`**:
  - **Body:** `{"refresh": "..."}`
  - **Returns:** A new `access` token.

### Inventory (`/api/inventory/`)
Standard CRUD (Create, Read, Update, Delete) endpoints are available. Data is automatically filtered by the authenticated user's `store`.

- **Products:**
  - `GET /api/inventory/products/` (List all products)
  - `POST /api/inventory/products/` (Create a new product)
  - `GET /api/inventory/products/{id}/` (Retrieve one product)
  - `PUT/PATCH /api/inventory/products/{id}/` (Update a product)
  - `DELETE /api/inventory/products/{id}/` (Delete a product)
- **Categories:**
  - `GET /api/inventory/categories/`
  - `POST /api/inventory/categories/`
  - etc.
- **Suppliers:**
  - `GET /api/inventory/suppliers/`
  - `POST /api/inventory/suppliers/`
  - etc.

---

## 4. How to Use with GitHub Copilot Chat

1. Open the Chat view in VS Code.
2. Type `@workspace` followed by your question. This tells Copilot to read all the files in your project, including this `README.md`.

**Example Questions:**
- `@workspace What fields are in the Product model?`
- `@workspace Generate a DRF serializer for the Customer model.`
- `@workspace How do I get an authentication token?`
- `@workspace Explain the purpose of the `attributes` field in the Product model.`