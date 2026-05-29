from django.urls import path

from .views import (
    SalesReportView, ProfitMarginView, ARAgingView, APAgingView,
    ProfitLossView, ExpenseReportView, StockLedgerView,
    CashierPerformanceView, TaxReportView,
)

urlpatterns = [
    path('sales/', SalesReportView.as_view(), name='report-sales'),
    path('profit-margin/', ProfitMarginView.as_view(), name='report-profit-margin'),
    path('ar-aging/', ARAgingView.as_view(), name='report-ar-aging'),
    path('ap-aging/', APAgingView.as_view(), name='report-ap-aging'),
    path('pnl/', ProfitLossView.as_view(), name='report-pnl'),
    path('expenses/', ExpenseReportView.as_view(), name='report-expenses'),
    path('stock-ledger/', StockLedgerView.as_view(), name='report-stock-ledger'),
    path('cashier-performance/', CashierPerformanceView.as_view(), name='report-cashier'),
    path('tax/', TaxReportView.as_view(), name='report-tax'),
]
