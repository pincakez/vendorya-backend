from django.urls import path
from .views import VendoryaTokenObtainView, MeView

urlpatterns = [
    path('token/', VendoryaTokenObtainView.as_view(), name='token_obtain_pair'),
    path('me/', MeView.as_view(), name='me'),
]
