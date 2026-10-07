from django.contrib import admin
from core.admin import SoftDeleteAdmin
from .models import PaymentMethod, ExpenseCategory

# §PRIVACY-SUDO (s163, Yakot 2026-10-08): the platform account never sees a shop's business data.
# Sales, payments, expenses, shifts, refunds and purchases are NOT registered in Django admin —
# only the shop's own setup lists (payment methods, expense categories) stay.

admin.site.register(PaymentMethod, SoftDeleteAdmin)
admin.site.register(ExpenseCategory, SoftDeleteAdmin)
