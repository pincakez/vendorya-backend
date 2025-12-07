from django.contrib import admin
from .models import Supplier, Category, Product, AttributeDefinition
from .forms import ProductForm # Import the form

class TenantAwareAdmin(admin.ModelAdmin):
    pass

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductForm # <--- Tell Django to use our Magic Form
    list_display = ('name', 'product_code', 'category', 'store', 'price', 'stock_quantity', 'profit')
    list_filter = ('status', 'category', 'store')
    search_fields = ('name', 'product_code')
    readonly_fields = ('profit',)
    
    # Hide the raw JSON field so you don't see the ugly box anymore
    exclude = ('attributes',)

@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'code_prefix', 'store')
    search_fields = ('name', 'code_prefix')

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'store')
    search_fields = ('name',)

@admin.register(AttributeDefinition)
class AttributeDefinitionAdmin(admin.ModelAdmin):
    list_display = ('name', 'key', 'input_type', 'store')
    list_filter = ('store', 'input_type')
    readonly_fields = ('key',)