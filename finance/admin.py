from django.contrib import admin
from .models import PaymentMethod, SalesInvoice, SalesInvoiceItem

class SalesInvoiceItemInline(admin.TabularInline):
    model = SalesInvoiceItem
    extra = 1
    readonly_fields = ('total_price',)

@admin.register(SalesInvoice)
class SalesInvoiceAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'store', 'total_amount', 'invoice_date')
    list_filter = ('store', 'invoice_date')
    search_fields = ('customer__name',)
    readonly_fields = ('subtotal_amount', 'total_amount')
    inlines = [SalesInvoiceItemInline]

admin.site.register(PaymentMethod)