from rest_framework import viewsets, permissions, filters
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView
from .models import User, Customer
from .serializers import VendoryaTokenObtainSerializer, UserProfileSerializer, CustomerSerializer, StaffSerializer


class VendoryaTokenObtainView(TokenObtainPairView):
    serializer_class = VendoryaTokenObtainSerializer


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserProfileSerializer(request.user).data)

    def patch(self, request):
        data = request.data
        user = request.user
        for field in ('first_name', 'last_name', 'email'):
            if field in data:
                setattr(user, field, data[field])
        password = data.get('password', '').strip()
        if password:
            user.set_password(password)
        user.save()
        return Response(UserProfileSerializer(user).data)


class CustomerViewSet(viewsets.ModelViewSet):
    serializer_class = CustomerSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'phone_number']

    def get_queryset(self):
        return Customer.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class StaffViewSet(viewsets.ModelViewSet):
    serializer_class = StaffSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['username', 'first_name', 'last_name', 'email']
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        return User.objects.filter(store=self.request.user.store).order_by('first_name', 'username')

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)
