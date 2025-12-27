from django.contrib import admin
from core.admin import SoftDeleteAdmin
from .models import (
    Supplier, Category, Product, ProductVariant, AttributeDefinition, 
    ProductAttribute, StockLevel, Tax, BundleItem, StockAdjustment
)

class ProductAttributeInline(admin.TabularInline):
    model = ProductAttribute
    extra = 1

class StockLevelInline(admin.TabularInline):
    model = StockLevel
    extra = 1

class BundleItemInline(admin.TabularInline):
    model = BundleItem
    fk_name = 'bundle'
    extra = 1

class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 1
    show_change_link = True
    exclude = ('is_deleted', 'deleted_at')

@admin.register(Product)
class ProductAdmin(SoftDeleteAdmin):
    list_display = ('name', 'store', 'product_type', 'category', 'supplier')
    list_filter = ('store', 'product_type', 'category')
    search_fields = ('name',)
    inlines = [ProductVariantInline, BundleItemInline]

@admin.register(ProductVariant)
class ProductVariantAdmin(SoftDeleteAdmin):
    list_display = ('product', 'sku', 'cost_price', 'sell_price')
    search_fields = ('sku', 'product__name')
    inlines = [ProductAttributeInline, StockLevelInline]

@admin.register(StockLevel)
class StockLevelAdmin(admin.ModelAdmin):
    list_display = ('variant', 'branch', 'quantity')
    list_filter = ('branch',)

@admin.register(Supplier)
class SupplierAdmin(SoftDeleteAdmin):
    list_display = ('name', 'code_prefix', 'store')

@admin.register(Category)
class CategoryAdmin(SoftDeleteAdmin):
    list_display = ('name', 'parent', 'store')

@admin.register(AttributeDefinition)
class AttributeDefinitionAdmin(SoftDeleteAdmin):
    list_display = ('name', 'key', 'input_type', 'store')

@admin.register(Tax)
class TaxAdmin(SoftDeleteAdmin):
    list_display = ('name', 'rate', 'store')

@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    list_display = ('variant', 'branch', 'quantity_change', 'reason', 'adjusted_by', 'created_at')
    list_filter = ('reason', 'branch', 'created_at')
    search_fields = ('variant__sku', 'notes')