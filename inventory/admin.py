from django.contrib import admin
from core.admin import SoftDeleteAdmin
from .models import Category, AttributeDefinition, Tax

# §PRIVACY-SUDO (s163, Yakot 2026-10-08): the platform account never sees a shop's business data.
# Products, variants, stock, suppliers and stock adjustments are NOT registered in Django admin —
# only the shop's own setup lists (categories, attributes, taxes) stay.


@admin.register(Category)
class CategoryAdmin(SoftDeleteAdmin):
    list_display = ('name', 'parent', 'store')


@admin.register(AttributeDefinition)
class AttributeDefinitionAdmin(SoftDeleteAdmin):
    list_display = ('name', 'key', 'input_type', 'store')


@admin.register(Tax)
class TaxAdmin(SoftDeleteAdmin):
    list_display = ('name', 'rate', 'store')
