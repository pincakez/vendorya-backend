from django.contrib import admin
from .models import Supplier, Category, Product, AttributeDefinition, ProductAttribute

class ProductAttributeInline(admin.TabularInline):
    model = ProductAttribute
    extra = 1 # Show 1 empty row by default
    autocomplete_fields = ['definition'] # Makes searching for "Size" faster

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'product_code', 'category', 'store', 'price', 'stock_quantity', 'profit')
    list_filter = ('status', 'category', 'store')
    search_fields = ('name', 'product_code')
    readonly_fields = ('profit',)
    inlines = [ProductAttributeInline] # <--- This adds the table inside the product page

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
    search_fields = ('name', 'key')
    readonly_fields = ('key',)