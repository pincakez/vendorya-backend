from django.contrib import admin

from .models import APIKey


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    list_display = ('label', 'key_prefix', 'store', 'is_active', 'expires_at', 'last_used_at')
    list_filter = ('is_active',)
    search_fields = ('label', 'key_prefix', 'store__name')
    readonly_fields = ('key_prefix', 'key_hash', 'last_used_at', 'created_at', 'updated_at')
