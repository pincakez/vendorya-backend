# Vendorya API Endpoints Reference

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
