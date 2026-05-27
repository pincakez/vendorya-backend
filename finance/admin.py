from django.contrib import admin
from core.admin import SoftDeleteAdmin
from .models import (
    PaymentMethod, SalesInvoice, SalesInvoiceItem, Payment, 
    InvoiceSequence, Expense, ExpenseCategory, WorkShift,
    RefundInvoice, RefundItem, PurchaseInvoice, PurchaseItem
)

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
    list_display = ('amount', 'method', 'invoice', 'created_by', 'created_at')
    list_filter = ('method', 'created_at')

@admin.register(Expense)
class ExpenseAdmin(SoftDeleteAdmin):
    list_display = ('description', 'amount', 'category', 'branch', 'date')
    list_filter = ('branch', 'category')

@admin.register(WorkShift)
class WorkShiftAdmin(admin.ModelAdmin):
    list_display = ('user', 'branch', 'start_time', 'status', 'difference')
    list_filter = ('status', 'branch')
    readonly_fields = ('difference', 'expected_cash', 'end_time')

class RefundItemInline(admin.TabularInline):
    model = RefundItem
    extra = 1

@admin.register(RefundInvoice)
class RefundInvoiceAdmin(SoftDeleteAdmin):
    list_display = ('refund_number', 'original_invoice', 'customer', 'total_refunded', 'date')
    list_filter = ('store', 'date')
    inlines = [RefundItemInline]

class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 1
    readonly_fields = ('total_cost',)

@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(SoftDeleteAdmin):
    list_display = ('supplier', 'vendor_reference', 'total_amount', 'status', 'date')
    list_filter = ('store', 'status', 'date')
    inlines = [PurchaseItemInline]

admin.site.register(PaymentMethod, SoftDeleteAdmin)
admin.site.register(InvoiceSequence)
admin.site.register(ExpenseCategory, SoftDeleteAdmin)