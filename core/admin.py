from django.contrib import admin
from django.urls import path, reverse
from django.utils.html import format_html
from .models import Store, Address, Branch
from .admin_views import store_global_search_view, store_global_search_api

class AddressInline(admin.StackedInline):
    model = Address
    extra = 0

class BranchInline(admin.TabularInline):
    model = Branch
    extra = 0

@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'plan', 'is_active', 'search_dashboard_link')
    list_filter = ('plan', 'is_active')
    search_fields = ('name', 'owner__username')
    inlines = [AddressInline, BranchInline]
    
    # 1. Add Custom URLs for the Search Page
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<uuid:store_id>/search/', self.admin_site.admin_view(store_global_search_view), name='store_global_search'),
            path('<uuid:store_id>/search/api/', self.admin_site.admin_view(store_global_search_api), name='store_global_search_api'),
        ]
        return custom_urls + urls

    # 2. Create the Button
    def search_dashboard_link(self, obj):
        url = reverse('admin:store_global_search', args=[obj.id])
        return format_html(
            '<a class="button" href="{}" style="background-color:#28a745; color:white; padding:5px 10px; border-radius:4px;">🔍 Search</a>', 
            url
        )
    search_dashboard_link.short_description = "Actions"

@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ('store', 'city', 'street_1')

@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ('name', 'store', 'is_main_branch')