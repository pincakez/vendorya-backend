from django.contrib import admin
from django.utils.html import format_html
from import_export.admin import ImportExportModelAdmin
from import_export import resources
from .models import Supplier, Category, Product, AttributeDefinition, ProductAttribute

# 1. EXCEL RESOURCE CONFIGURATION
class ProductResource(resources.ModelResource):
    class Meta:
        model = Product
        fields = ('id', 'product_code', 'name', 'price', 'wholesale_price', 'stock_quantity', 'category__name', 'supplier__name')

# 2. INLINE ATTRIBUTES
class ProductAttributeInline(admin.TabularInline):
    model = ProductAttribute
    extra = 1
    autocomplete_fields = ['definition']

# 3. PRODUCT ADMIN
@admin.register(Product)
class ProductAdmin(ImportExportModelAdmin): 
    resource_class = ProductResource
    inlines = [ProductAttributeInline]
    
    # COLUMNS
    list_display = (
        'product_code',
        'category_tooltip',
        'name_tooltip',
        'retail_price_col',
        'wholesale_price_col',
        'supplier',
        'stock_quantity'
    )
    
    # FILTERS & SEARCH
    list_filter = ('store', 'status', 'category', 'supplier')
    search_fields = ('name', 'product_code')
    readonly_fields = ('profit',)

    # CUSTOM COLUMN LOGIC
    def category_tooltip(self, obj):
        if not obj.category:
            return "-"  # Return a dash if no category
            
        full_path = str(obj.category)
        short_name = obj.category.name
        return format_html('<span title="{}">{}</span>', full_path, short_name)
    category_tooltip.short_description = 'Category'

    def name_tooltip(self, obj):
        attrs = obj.attributes.all()
        if attrs:
            tooltip_text = "\n".join([f"{a.definition.name}: {a.value}" for a in attrs])
        else:
            tooltip_text = "No attributes"
        return format_html('<span title="{}">{}</span>', tooltip_text, obj.name)
    name_tooltip.short_description = 'Product Name'

    def retail_price_col(self, obj):
        return obj.price
    retail_price_col.short_description = 'R-Price'

    def wholesale_price_col(self, obj):
        return obj.wholesale_price
    wholesale_price_col.short_description = 'W-Price'

# 4. OTHER ADMINS
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