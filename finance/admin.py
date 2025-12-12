from django.contrib import admin
from .models import PaymentMethod, SalesInvoice, SalesInvoiceItem, Payment

class SalesInvoiceItemInline(admin.TabularInline):
    model = SalesInvoiceItem
    extra = 1
    readonly_fields = ('total_price',)

class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = ('payment_date',)

@admin.register(SalesInvoice)
class SalesInvoiceAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'store', 'total_amount', 'invoice_date')
    list_filter = ('store', 'invoice_date')
    search_fields = ('customer__name',)
    readonly_fields = ('subtotal_amount', 'total_amount')
    inlines = [SalesInvoiceItemInline, PaymentInline]

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('amount', 'method', 'invoice', 'payment_date', 'payment_type')
    list_filter = ('payment_type', 'payment_date', 'method')
    search_fields = ('invoice__id', 'reference_number')

admin.site.register(PaymentMethod)