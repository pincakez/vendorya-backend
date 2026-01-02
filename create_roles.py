import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vendorya_project.settings')
django.setup()

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

def create_group(name, models_permissions):
    group, created = Group.objects.get_or_create(name=name)
    print(f"Processing Group: {name}")
    
    for model_name, perms in models_permissions.items():
        # Find the content type (e.g., 'product')
        try:
            # We search by model name (case insensitive)
            ct = ContentType.objects.filter(model=model_name.lower()).first()
            if not ct:
                print(f"  - Warning: Model '{model_name}' not found.")
                continue
                
            for perm_code in perms:
                codename = f"{perm_code}_{model_name.lower()}"
                try:
                    p = Permission.objects.get(content_type=ct, codename=codename)
                    group.permissions.add(p)
                    print(f"  + Added: {codename}")
                except Permission.DoesNotExist:
                    print(f"  - Error: Permission '{codename}' not found.")
                    
        except Exception as e:
            print(f"Error: {e}")

# --- DEFINING ROLES ---

# 1. CASHIER
cashier_perms = {
    'SalesInvoice': ['add', 'view', 'change'],
    'SalesInvoiceItem': ['add', 'view'],
    'Payment': ['add', 'view'],
    'Customer': ['add', 'view', 'change'],
    'Product': ['view'],
    'ProductVariant': ['view'],
    'StockLevel': ['view'],
    'WorkShift': ['add', 'view', 'change'], # Open/Close Shift
    'RefundInvoice': ['add', 'view'],
}

# 2. MANAGER
manager_perms = cashier_perms.copy()
manager_perms.update({
    'Product': ['add', 'change', 'view'],
    'ProductVariant': ['add', 'change', 'view'],
    'Supplier': ['add', 'change', 'view'],
    'StockAdjustment': ['add', 'view'], # Can fix stock
    'Expense': ['add', 'view'],
    'User': ['view'], # Can see staff list
})

# 3. OWNER
owner_perms = manager_perms.copy()
owner_perms.update({
    'Store': ['change', 'view'], # Edit settings
    'Branch': ['add', 'change', 'delete', 'view'],
    'User': ['add', 'change', 'delete', 'view'], # Manage staff
    'ActivityLog': ['view'], # See audit logs
    'Tax': ['add', 'change', 'delete', 'view'],
    'AttributeDefinition': ['add', 'change', 'delete', 'view'],
})

# --- EXECUTE ---
create_group('Cashier', cashier_perms)
create_group('Manager', manager_perms)
create_group('Store Owner', owner_perms)

print("Done! Roles created.")
