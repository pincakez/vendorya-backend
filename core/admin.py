from django.contrib import admin
from .models import Store, Address, Branch, StoreSettings

# --- BASE ADMIN CLASS (Security + Soft Delete) ---
class SoftDeleteAdmin(admin.ModelAdmin):
    exclude = ('is_deleted', 'deleted_at')

    def get_form(self, request, obj=None, **kwargs):
        """Hide the 'store' field for non-superusers."""
        form = super().get_form(request, obj, **kwargs)
        if not request.user.is_superuser:
            if 'store' in form.base_fields:
                form.base_fields.pop('store') # Remove it from the form
        return form

    # ... keep get_queryset and save_model as they were ...
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        
        # 1. Filter out deleted items (Fixes the "Deleted Products" bug)
        if hasattr(qs.model, 'is_deleted'):
            qs = qs.filter(is_deleted=False)

        if request.user.is_superuser:
            return qs
            
        # 2. Security Filter (The Smart Part)
        if request.user.store:
            # Case A: Model has direct 'store' field (Product, Supplier)
            if hasattr(qs.model, 'store'):
                return qs.filter(store=request.user.store)
            
            # Case B: Model is a Variant (look at product__store)
            elif hasattr(qs.model, 'product'):
                return qs.filter(product__store=request.user.store)
            
            # Case C: Model is StockLevel (look at branch__store)
            elif hasattr(qs.model, 'branch'):
                return qs.filter(branch__store=request.user.store)
                
        return qs.none()

class AddressInline(admin.StackedInline):
    model = Address
    extra = 0
    exclude = ('is_deleted', 'deleted_at')

class BranchInline(admin.TabularInline):
    model = Branch
    extra = 0
    exclude = ('is_deleted', 'deleted_at')

@admin.register(Store)
class StoreAdmin(SoftDeleteAdmin):
    list_display = ('name', 'owner', 'plan', 'is_active')
    list_filter = ('plan', 'is_active')
    search_fields = ('name', 'owner__username')
    inlines = [AddressInline, BranchInline]
    
    fields = (
        'name', 'owner', 'plan', 'is_active', 'default_supplier',
        'default_category', 'default_language', 'currency', 'timezone',
    )
    readonly_fields = ('created_at', 'updated_at')

    # FIX: Explicitly allow owners to see their store
    def get_queryset(self, request):
        # Bypass the parent SoftDeleteAdmin logic completely for Stores
        qs = self.model.objects.filter(is_deleted=False)
        
        if request.user.is_superuser:
            return qs
            
        # Explicitly show stores owned by the user
        return qs.filter(owner=request.user)

    # §PRIVACY-SUDO (s163): the per-store "Search" page (products, suppliers, customers + phones) was removed.


@admin.register(Address)
class AddressAdmin(SoftDeleteAdmin):
    list_display = ('store', 'city', 'street_1')

@admin.register(Branch)
class BranchAdmin(SoftDeleteAdmin):
    list_display = ('name', 'store', 'is_main_branch')
    search_fields = ('name',)

# §PRIVACY-SUDO (s163): ActivityLog is NOT registered — its details carry amounts and customer names.

@admin.register(StoreSettings)
class StoreSettingsAdmin(SoftDeleteAdmin):
    list_display = ('store', 'allow_negative_stock', 'enable_agel_selling')
    search_fields = ('store__name', 'tax_id')
    
    # Security: Only show my store's settings
    def get_queryset(self, request):
        qs = super().get_queryset(request) # This already handles the store filter via SoftDeleteAdmin logic?
        # Wait, SoftDeleteAdmin logic looks for 'store' field.
        # StoreSettings has 'store' field. So it should work automatically!
        return qs