from django.contrib import admin
from .models import Supplier, Category, Product, ProductVariant, AttributeDefinition, ProductAttribute, StockLevel, Tax, BundleItem

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

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'store', 'product_type', 'category', 'supplier')
    list_filter = ('store', 'product_type', 'category')
    search_fields = ('name',)
    inlines = [ProductVariantInline, BundleItemInline]

@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ('product', 'sku', 'cost_price', 'sell_price')
    search_fields = ('sku', 'product__name')
    inlines = [ProductAttributeInline, StockLevelInline]

@admin.register(StockLevel)
class StockLevelAdmin(admin.ModelAdmin):
    list_display = ('variant', 'branch', 'quantity')
    list_filter = ('branch',)

admin.site.register(Supplier)
admin.site.register(Category)
admin.site.register(AttributeDefinition)
admin.site.register(Tax)