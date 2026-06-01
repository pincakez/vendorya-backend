"""Sudo-only "Trash" — list and restore soft-deleted records per store.

Soft deletes (`is_deleted=True`) are recoverable by design (see SoftDeleteModel).
This exposes them to the super-admin: pick a store, see what was deleted across
the common per-store models, and restore individual records.

All models registered here have a direct `store` FK and inherit SoftDeleteModel,
so listing is `all_objects.filter(is_deleted=True, store=...)` and restoring is
`obj.restore()`. Everything is store-scoped and sudo-gated.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from users.permissions import IsSuperAdmin
from .activity import log_activity
from .models import ActivityLog, Store, Branch
from inventory.models import Product, Category, Supplier, AttributeDefinition, Tax
from users.models import Customer
from finance.models import (
    Expense, ExpenseCategory, PaymentMethod,
    SalesInvoice, PurchaseInvoice, RefundInvoice,
)


def _invoice_label(obj):
    return f"#{obj.invoice_number}" if obj.invoice_number else "(unposted)"


# slug -> (Model, human label, label callable)
TRASH_REGISTRY = {
    'product':          (Product,            'Products',         lambda o: o.name),
    'category':         (Category,           'Categories',       lambda o: o.name),
    'supplier':         (Supplier,           'Suppliers',        lambda o: o.name),
    'attribute':        (AttributeDefinition,'Attributes',       lambda o: o.name),
    'tax':              (Tax,                'Taxes',            lambda o: o.name),
    'customer':         (Customer,           'Customers',        lambda o: o.name),
    'expense':          (Expense,            'Expenses',         lambda o: o.description or str(o.amount)),
    'expense_category': (ExpenseCategory,    'Expense Categories', lambda o: o.name),
    'payment_method':   (PaymentMethod,      'Payment Methods',  lambda o: o.name),
    'sales_invoice':    (SalesInvoice,       'Sales Invoices',   _invoice_label),
    'purchase_invoice': (PurchaseInvoice,    'Purchases',        lambda o: o.invoice_number or '(draft)'),
    'refund':           (RefundInvoice,      'Refunds',          lambda o: f"#{o.id}"[:10]),
    'branch':           (Branch,             'Branches',         lambda o: o.name),
}


class AdminTrashListView(APIView):
    """GET /api/admin/trash/?store=<uuid> — soft-deleted records for one store,
    grouped by model. Returns groups with their deleted rows (id, label, when)."""
    permission_classes = [IsSuperAdmin]

    def get(self, request):
        store_id = request.query_params.get('store')
        if not store_id:
            return Response({'detail': 'store query param is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            store = Store.all_objects.get(pk=store_id)
        except (Store.DoesNotExist, ValueError, TypeError):
            return Response({'detail': 'Store not found.'}, status=status.HTTP_404_NOT_FOUND)

        groups = []
        for slug, (Model, label, label_fn) in TRASH_REGISTRY.items():
            qs = Model.all_objects.filter(is_deleted=True, store=store).order_by('-deleted_at')
            rows = [{
                'id': str(obj.pk),
                'label': label_fn(obj),
                'deleted_at': getattr(obj, 'deleted_at', None),
            } for obj in qs]
            if rows:
                groups.append({'model': slug, 'label': label, 'count': len(rows), 'rows': rows})

        return Response({'store': {'id': str(store.id), 'name': store.name}, 'groups': groups})


class AdminTrashRestoreView(APIView):
    """POST /api/admin/trash/restore/ {model, id} — un-delete one record."""
    permission_classes = [IsSuperAdmin]

    def post(self, request):
        slug = request.data.get('model')
        obj_id = request.data.get('id')
        entry = TRASH_REGISTRY.get(slug)
        if not entry:
            return Response({'detail': f'Unknown model "{slug}".'},
                            status=status.HTTP_400_BAD_REQUEST)
        Model, label, label_fn = entry
        try:
            obj = Model.all_objects.get(pk=obj_id, is_deleted=True)
        except (Model.DoesNotExist, ValueError, TypeError):
            return Response({'detail': 'Record not found or not deleted.'},
                            status=status.HTTP_404_NOT_FOUND)

        obj.restore()
        log_activity(
            request=request,
            action=f"Restored {label[:-1] if label.endswith('s') else label}: {label_fn(obj)}",
            op_type=ActivityLog.OperationType.OTHER,
            store=getattr(obj, 'store', None),
            details={'model': slug, 'id': str(obj.pk)},
        )
        return Response({'detail': 'Restored.', 'model': slug, 'id': str(obj.pk)})
