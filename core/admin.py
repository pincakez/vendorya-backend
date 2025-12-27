from django.contrib import admin
from django.urls import path, reverse
from django.utils.html import format_html
from .models import Store, Address, Branch
from .admin_views import store_global_search_view, store_global_search_api

# --- BASE ADMIN CLASS (Hides Deleted Fields) ---
class SoftDeleteAdmin(admin.ModelAdmin):
    exclude = ('is_deleted', 'deleted_at')

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
    list_display = ('name', 'owner', 'plan', 'is_active', 'search_dashboard_link')
    list_filter = ('plan', 'is_active')
    search_fields = ('name', 'owner__username')
    inlines = [AddressInline, BranchInline]
    
    # Hide technical fields
    fields = (
        'name', 'owner', 'plan', 'is_active', 'default_supplier', 
        'default_category', 'default_language', 'currency_symbol'
    )
    readonly_fields = ('created_at', 'updated_at')

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<uuid:store_id>/search/', self.admin_site.admin_view(store_global_search_view), name='store_global_search'),
            path('<uuid:store_id>/search/api/', self.admin_site.admin_view(store_global_search_api), name='store_global_search_api'),
        ]
        return custom_urls + urls

    def search_dashboard_link(self, obj):
        url = reverse('admin:store_global_search', args=[obj.id])
        return format_html(
            '<a class="button" href="{}" style="background-color:#28a745; color:white; padding:5px 10px; border-radius:4px;">🔍 Search</a>', 
            url
        )
    search_dashboard_link.short_description = "Actions"

@admin.register(Address)
class AddressAdmin(SoftDeleteAdmin):
    list_display = ('store', 'city', 'street_1')

@admin.register(Branch)
class BranchAdmin(SoftDeleteAdmin):
    list_display = ('name', 'store', 'is_main_branch')