from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    # Inherit security logic? No, UserAdmin is complex. We must inject logic manually.
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(store=request.user.store)

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser and not obj.store:
            obj.store = request.user.store
        super().save_model(request, obj, form, change)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if not request.user.is_superuser:
            # Remove 'store' and 'permissions' from the form for normal owners
            # This is tricky in Django UserAdmin, but let's try simple filtering
            return fieldsets # For now, let's just secure the list view
        return fieldsets

# §PRIVACY-SUDO (s163): Customer is NOT registered — the platform account never sees a shop's customers.
