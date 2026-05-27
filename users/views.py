from rest_framework import viewsets, filters
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView
from .models import User, Customer
from .permissions import RoleScopedPermission
from .serializers import VendoryaTokenObtainSerializer, UserProfileSerializer, CustomerSerializer, StaffSerializer
from core.activity import log_activity
from core.models import ActivityLog


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
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'CASHIER',
        'update':         'CASHIER',
        'partial_update': 'CASHIER',
        'destroy':        'MANAGER',
    }
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'phone_number']

    def get_queryset(self):
        return Customer.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class StaffViewSet(viewsets.ModelViewSet):
    serializer_class = StaffSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'MANAGER',
        'retrieve':       'MANAGER',
        'create':         'ADMIN',
        'update':         'ADMIN',
        'partial_update': 'ADMIN',
        'destroy':        'OWNER',
    }
    filter_backends = [filters.SearchFilter]
    search_fields = ['username', 'first_name', 'last_name', 'email']
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        return User.objects.filter(store=self.request.user.store).order_by('first_name', 'username')

    def perform_create(self, serializer):
        staff = serializer.save(store=self.request.user.store)
        log_activity(
            request=self.request,
            action=f"Added staff member: {staff.username}",
            op_type=ActivityLog.OperationType.STAFF,
            details={'staff_id': staff.id, 'username': staff.username, 'role': staff.role},
        )

    def perform_update(self, serializer):
        was_active = serializer.instance.is_active
        staff = serializer.save()
        if was_active and not staff.is_active:
            action = f"Deactivated staff member: {staff.username}"
        elif not was_active and staff.is_active:
            action = f"Reactivated staff member: {staff.username}"
        else:
            action = f"Updated staff member: {staff.username}"
        log_activity(
            request=self.request,
            action=action,
            op_type=ActivityLog.OperationType.STAFF,
            details={'staff_id': staff.id, 'username': staff.username, 'role': staff.role, 'is_active': staff.is_active},
        )
