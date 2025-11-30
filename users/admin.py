from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Customer

class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ('Store Info', {'fields': ('store', 'role', 'photo')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Store Info', {'fields': ('store', 'role', 'photo')}),
    )
    list_display = ('username', 'email', 'store', 'role', 'is_staff')
    list_filter = ('store', 'role', 'is_staff', 'is_superuser')

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone_number', 'store', 'created_at')
    search_fields = ('name', 'phone_number')
    list_filter = ('store',)

admin.site.register(User, CustomUserAdmin)