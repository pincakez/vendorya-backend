from axes.handlers.proxy import AxesProxyHandler
from django.conf import settings as dj_settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import viewsets, filters, serializers, status
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .models import User, Customer
from .permissions import RoleScopedPermission
from .serializers import VendoryaTokenObtainSerializer, UserProfileSerializer, CustomerSerializer, StaffSerializer
from .throttling import LoginRateThrottle
from .twofa import is_enrolled, verify_token
from .cookies import set_refresh_cookie, clear_refresh_cookie
from core.activity import log_activity
from core.models import ActivityLog
from core.security import get_client_ip, ip_allowed
from users.lockout import lockout_response


class VendoryaTokenObtainView(TokenObtainPairView):
    """Login. Layers, in order: rate-limit -> lockout -> password -> IP allowlist
    -> 2FA -> issue tokens + set refresh cookie."""
    serializer_class = VendoryaTokenObtainSerializer
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]
    throttle_scope = 'login'

    def post(self, request, *args, **kwargs):
        username = request.data.get('username')
        password = request.data.get('password')
        credentials = {'username': username, 'password': password}
        ip = get_client_ip(request)

        # 1. django-axes lockout (per username+IP). Proactive check -> JSON 429.
        if AxesProxyHandler().is_locked(request, credentials):
            return lockout_response(request, credentials)

        # 2. Password. SimpleJWT calls Django's authenticate() (with request), so
        #    axes records the failed attempt automatically via user_login_failed.
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.user

        # Password OK -> clear the failed-attempt counter for this username+IP.
        AxesProxyHandler().reset_attempts(username=username, ip_address=ip)

        # 3. IP allowlist (OWNER/ADMIN only, per-store). Sudo (no store) skipped.
        store = getattr(user, 'store', None)
        if store and user.role in (User.Role.OWNER, User.Role.ADMIN):
            allowlist = getattr(getattr(store, 'settings', None), 'login_ip_allowlist', '')
            if not ip_allowed(allowlist, ip):
                return Response(
                    {'detail': 'Login from this IP address is not allowed for your account.',
                     'code': 'ip_not_allowed'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # 4. Two-factor — OPTIONAL. Only users who voluntarily enrolled a
        #    confirmed device are prompted; nobody is ever forced to enrol.
        if is_enrolled(user):
            otp_token = str(request.data.get('otp_token', '')).strip()
            if not otp_token:
                return Response(
                    {'requires_2fa': True, 'detail': 'Enter your authenticator code.'},
                    status=status.HTTP_200_OK,
                )
            if not verify_token(user, otp_token):
                return Response(
                    {'detail': 'Invalid two-factor code.', 'code': 'invalid_otp'},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

        # 5. Success -> tokens. Refresh goes in the body (dual-support) AND as an
        #    httpOnly cookie (phased migration).
        data = serializer.validated_data
        response = Response(data, status=status.HTTP_200_OK)
        set_refresh_cookie(response, data['refresh'])
        return response


class CookieTokenRefreshView(TokenRefreshView):
    """Refresh accepts the token from the body OR the httpOnly cookie, and
    re-sets the rotated refresh cookie."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        if not request.data.get('refresh'):
            cookie = request.COOKIES.get(dj_settings.REFRESH_COOKIE_NAME)
            if cookie:
                # request.data may be immutable (QueryDict) — copy to inject.
                data = request.data.copy()
                data['refresh'] = cookie
                request._full_data = data
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200 and response.data.get('refresh'):
            set_refresh_cookie(response, response.data['refresh'])
        return response


class LogoutView(APIView):
    """Blacklist the presented refresh token (body or cookie) and clear the cookie."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get('refresh') or request.COOKIES.get(dj_settings.REFRESH_COOKIE_NAME)
        if token:
            try:
                RefreshToken(token).blacklist()
            except TokenError:
                pass  # already expired/blacklisted — treat as logged out
        response = Response({'detail': 'Logged out.'}, status=status.HTTP_200_OK)
        clear_refresh_cookie(response)
        return response


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
            try:
                validate_password(password, user)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({'password': list(exc.messages)})
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
