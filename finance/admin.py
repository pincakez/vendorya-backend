from django.contrib import admin
from core.admin import SoftDeleteAdmin
from .models import PaymentMethod, SalesInvoice, SalesInvoiceItem, Payment, InvoiceSequence, Expense, ExpenseCategory

class SalesInvoiceItemInline(admin.TabularInline):
    model = SalesInvoiceItem
    extra = 1
    readonly_fields = ('total',)

class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    exclude = ('is_deleted', 'deleted_at')

@admin.register(SalesInvoice)
class SalesInvoiceAdmin(SoftDeleteAdmin):
    list_display = ('invoice_number', 'customer', 'store', 'grand_total', 'status', 'date')
    list_filter = ('store', 'status', 'date')
    search_fields = ('invoice_number', 'customer__name')
    readonly_fields = ('invoice_number', 'grand_total', 'paid_amount')
    inlines = [SalesInvoiceItemInline, PaymentInline]

@admin.register(Payment)
class PaymentAdmin(SoftDeleteAdmin):
    list_display = ('amount', 'method', 'invoice', 'created_at')
    list_filter = ('method', 'created_at')

@admin.register(Expense)
class ExpenseAdmin(SoftDeleteAdmin):
    list_display = ('description', 'amount', 'category', 'branch', 'date')
    list_filter = ('branch', 'category')

admin.site.register(PaymentMethod, SoftDeleteAdmin)
admin.site.register(InvoiceSequence)
admin.site.register(ExpenseCategory, SoftDeleteAdmin)