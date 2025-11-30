from django.contrib import admin
from .models import Supplier, Category, Product

class TenantAwareAdmin(admin.ModelAdmin):
    """Base admin class to filter data by store (optional for superuser, mandatory for staff)."""
    pass # We keep it simple for now as you are the superuser

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'product_code', 'category', 'store', 'status', 'stock_quantity', 'price')
    list_filter = ('status', 'category', 'store')
    search_fields = ('name', 'product_code')

@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'code_prefix', 'store')
    search_fields = ('name', 'code_prefix')

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'store')
    search_fields = ('name',)