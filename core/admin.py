from django.contrib import admin
from .models import Store, Address, Branch

@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'plan', 'is_active')
    list_filter = ('plan', 'is_active')
    search_fields = ('name', 'owner__username')

admin.site.register(Address)
admin.site.register(Branch)