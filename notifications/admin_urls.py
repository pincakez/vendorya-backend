from django.urls import path
from .views import AdminAlertView, AdminAlertHistoryView

urlpatterns = [
    path('send/',    AdminAlertView.as_view(),        name='admin-alert-send'),
    path('history/', AdminAlertHistoryView.as_view(), name='admin-alert-history'),
]
