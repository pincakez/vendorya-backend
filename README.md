VENDORYA ERP - PROJECT MEMORY (v3.0)

System Context: Local WSL2 (Ubuntu 24.04) | Windows 11 Host
Role: Senior Full-Stack Architect & UI/UX Designer
Current State: Backend Restored & Running | Frontend Initialized (Blank Canvas)
1. The Architecture (Headless SaaS)

    Root Path: ~/vendorya/

    Backend: ~/vendorya/vendorya-backend/ (Django 5.2, Python 3.12, DRF) -> Port 8000

    Frontend: ~/vendorya/vendorya-frontend/ (Vue 3, Vite, Tailwind v4) -> Port 5173

    Database: PostgreSQL 16 (Local Service) -> Port 5432

2. 🚨 CRITICAL SECURITY PROTOCOL (The "Middleware Gap")

Current Risk: High.

    The Issue: The system uses a Shared Database schema (all tenants in one table). However, there is currently NO Global Middleware to automatically filter data by Tenant (store_id).

    The Rule: Until a ThreadLocal or django-multitenant middleware is implemented, EVERY Django ViewSet and API endpoint MUST explicitly include:
    code Python

    queryset = Model.objects.filter(store=self.request.user.store)

    Strict Warning: Failure to include this filter will result in a Data Leak (Store A seeing Store B's data). This is the #1 priority for code review.

3. Backend Engineering Rules

    Soft Deletes: Data is never deleted. All models inherit SoftDeleteModel.

    Inventory Logic:

        Product (Abstract Parent) -> ProductVariant (Sellable SKU) -> StockLevel (Physical Count).

        No Magic Stock: Stock is only updated via PurchaseInvoice (Supply) or StockAdjustment (Audit).

    Finance Logic:

        Shift Enforcement: Cashiers cannot sell without an OPEN WorkShift. API returns 403 Forbidden.

        Invoice Sequence: Per-store sequential IDs (1001, 1002), not UUIDs.

4. Frontend Engineering Rules (The "Designer's Canvas")

    Stack: Vue 3 (Composition API), Pinia, Vue Router, Axios.

    Styling: Tailwind CSS v4 (configured via @tailwindcss/postcss).

    UI Components: @headlessui/vue (Logic only), lucide-vue-next (Icons).

    Data Tables: @tanstack/vue-table (Headless sorting/filtering).

    Design Philosophy: Pixel-perfect implementation of the Photoshop concepts. Dark mode default.

5. The "Raw Linux" Workflow

The user prefers manual terminal commands to build muscle memory.

    Start Backend:
    code Bash

    cd ~/vendorya/vendorya-backend
    source venv/bin/activate
    python manage.py runserver

    Start Frontend:
    code Bash

    cd ~/vendorya/vendorya-frontend
    npm run dev

    Database Control: sudo service postgresql start / stop

6. Immediate Task List

    Restore Database & Backend from AWS backup.

    Initialize Vue 3 Frontend with Tailwind v4.

    Build "Sidebar" Component: Create a responsive, dark-mode sidebar using Lucide icons and Vue Router links.

