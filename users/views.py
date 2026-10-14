from axes.handlers.proxy import AxesProxyHandler
from django.conf import settings as dj_settings
from django.db import transaction
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import viewsets, filters, serializers, status
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .models import User, Customer
from .permissions import RoleScopedPermission, ROLE_RANK, _user_rank
from rest_framework.exceptions import PermissionDenied
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

        # 5. Success -> tokens. The refresh token goes ONLY into the httpOnly
        #    cookie — never the response body — so page JS (XSS) can't read it.
        #    The body keeps just the short-lived access token + user payload.
        data = dict(serializer.validated_data)
        refresh = data.pop('refresh', None)
        response = Response(data, status=status.HTTP_200_OK)
        if refresh:
            set_refresh_cookie(response, refresh)
        return response


class CookieTokenRefreshView(TokenRefreshView):
    """Refresh reads the token from the httpOnly cookie (body still accepted as a
    fallback), rotates it into a new cookie, and returns ONLY the access token in
    the body — the rotated refresh token never reaches page JS."""
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
        if response.status_code == 200:
            # ROTATE_REFRESH_TOKENS returns a new refresh token — move it into the
            # cookie and strip it from the body so JS only ever sees the access token.
            rotated = response.data.pop('refresh', None)
            if rotated:
                set_refresh_cookie(response, rotated)
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
        for field in ('first_name', 'last_name', 'email', 'phone_number', 'whatsapp_number'):
            if field in data:
                setattr(user, field, data[field])
        if 'photo' in request.FILES:
            user.photo = request.FILES['photo']

        # Was ignored, so the reply ('en') flipped the app back to English right after picking Arabic (s156, §AUDIT B1).
        if 'language' in data:
            if data['language'] not in User.Language.values:
                return Response({'language': 'Unknown language.'}, status=status.HTTP_400_BAD_REQUEST)
            user.language = data['language']

        if 'default_branch' in data:
            branch_id = data['default_branch']
            if branch_id is None:
                user.default_branch = None
            else:
                from core.models import Branch
                branch = Branch.objects.filter(id=branch_id, store=user.store).first()
                if not branch:
                    return Response({'default_branch': 'Branch not found or does not belong to your store.'},
                                    status=status.HTTP_400_BAD_REQUEST)
                user.default_branch = branch

        if 'pos_settings' in data:
            if not isinstance(data['pos_settings'], dict):
                return Response({'pos_settings': 'Must be a JSON object.'}, status=status.HTTP_400_BAD_REQUEST)
            user.pos_settings = data['pos_settings']

        if 'ui_prefs' in data:
            if not isinstance(data['ui_prefs'], dict):
                return Response({'ui_prefs': 'Must be a JSON object.'}, status=status.HTTP_400_BAD_REQUEST)
            user.ui_prefs = data['ui_prefs']

        user.save()
        return Response(UserProfileSerializer(user).data)


class ApplyPosSettingsView(APIView):
    """Copy the requesting user's pos_settings to all staff in the same store.
    Only OWNER or ADMIN may broadcast settings."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if user.role not in (User.Role.OWNER, User.Role.ADMIN):
            return Response({'detail': 'Only owners and admins can apply settings to all staff.'},
                            status=status.HTTP_403_FORBIDDEN)
        if not user.store:
            return Response({'detail': 'No store associated with this account.'},
                            status=status.HTTP_400_BAD_REQUEST)
        settings_to_apply = user.pos_settings
        updated = User.objects.filter(store=user.store).exclude(id=user.id).update(pos_settings=settings_to_apply)
        return Response({'applied_to': updated})


class ChangePasswordView(APIView):
    """Self-service password change. Requires the current password.

    Also used for the forced change after an admin issues a temp password —
    on success the force_password_change flag is cleared.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        current = str(request.data.get('current_password', ''))
        new = str(request.data.get('new_password', '')).strip()
        # Forced first-login change: the user just authenticated with the temp
        # password, so we don't demand it again. Voluntary changes require it.
        if not user.force_password_change and not user.check_password(current):
            return Response({'detail': 'Current password is incorrect.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not new:
            return Response({'detail': 'New password is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_password(new, user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'new_password': list(exc.messages)})
        with transaction.atomic():
            user.set_password(new)
            user.force_password_change = False
            user.save(update_fields=['password', 'force_password_change'])
        return Response({'detail': 'Password changed.'})


def with_live_balance(qs):
    """Annotate a Customer queryset with `live_balance` in ONE query (s165, §A36) — the same number as
    finance.models.customer_outstanding(), which used to run once per row on the Customers list.

    balance = opening seed + Σ over POSTED, non-deleted sales of (grand_total − paid_amount)
                          + Σ over their live returns of (− total_refunded + restocking_fee + paid_back)
                          − Σ over their live lump-sum receipts of the part that paid no sale (s167)

    The sums are SEPARATE correlated subqueries on purpose: joining sales to returns in one SUM
    would count a sale's total once per return (join multiplication)."""
    from decimal import Decimal
    from django.db.models import DecimalField, ExpressionWrapper, F, OuterRef, Subquery, Sum, Value
    from django.db.models.functions import Coalesce
    from finance.models import SalesInvoice, RefundInvoice, CustomerReceipt
    DEC = DecimalField(max_digits=14, decimal_places=2)
    ZERO = Value(Decimal('0'), output_field=DEC)
    posted = {'status': SalesInvoice.Status.POSTED, 'is_deleted': False}
    sales = (SalesInvoice.all_objects   # the customer rows are already store-filtered
             .filter(customer=OuterRef('pk'), **posted)
             .order_by().values('customer')
             .annotate(s=Sum(F('grand_total') - F('paid_amount'), output_field=DEC))
             .values('s'))
    returns = (RefundInvoice.all_objects
               .filter(original_invoice__customer=OuterRef('pk'), is_deleted=False,
                       **{f'original_invoice__{k}': v for k, v in posted.items()})
               .order_by().values('original_invoice__customer')
               .annotate(s=Sum(F('restocking_fee') + F('paid_back') - F('total_refunded'), output_field=DEC))
               .values('s'))
    receipts = (CustomerReceipt.all_objects
                .filter(customer=OuterRef('pk'), is_deleted=False)
                .order_by().values('customer')
                .annotate(s=Sum('unallocated', output_field=DEC))
                .values('s'))
    return qs.annotate(live_balance=ExpressionWrapper(
        Coalesce(F('balance'), ZERO, output_field=DEC)
        + Coalesce(Subquery(sales, output_field=DEC), ZERO, output_field=DEC)
        + Coalesce(Subquery(returns, output_field=DEC), ZERO, output_field=DEC)
        - Coalesce(Subquery(receipts, output_field=DEC), ZERO, output_field=DEC),
        output_field=DEC,
    ))


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
        'invoices':       'CASHIER',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'phone_number']
    # 'balance' is computed live (not a real DB column to order by), so it's
    # intentionally absent here — the Customers table shows it un-sortable.
    ordering_fields = ['name', 'phone_number', 'store_credit', 'created_at']
    ordering = ['name']

    def get_queryset(self):
        qs = Customer.objects.filter(store=self.request.user.store)
        if self.action in ('list', 'retrieve'):
            qs = with_live_balance(qs)   # §A36: no per-row balance query on the list
        walk = self.request.query_params.get('is_walk_in')
        if walk is not None:
            qs = qs.filter(is_walk_in=walk.lower() in ('true', '1', 'yes'))
        return qs

    def perform_create(self, serializer):
        customer = serializer.save(store=self.request.user.store)
        if customer.balance:
            self._log_opening(customer, None)

    def perform_update(self, serializer):
        before = serializer.instance.balance
        customer = serializer.save()
        if customer.balance != before:
            self._log_opening(customer, before)

    def _log_opening(self, customer, before):
        """An opening balance moves what the customer owes — keep a trail (s167)."""
        from core.activity import log_activity
        from core.models import ActivityLog
        log_activity(
            request=self.request,
            action=f"Set opening balance of {customer.name} to {customer.balance}",
            op_type=ActivityLog.OperationType.OTHER,
            details={'customer_id': str(customer.id), 'customer': customer.name,
                     'from': str(before) if before is not None else None, 'to': str(customer.balance)},
        )

    @action(detail=True, methods=['get'])
    def invoices(self, request, pk=None):
        from rest_framework import serializers as drf_serializers
        from finance.models import SalesInvoice, Payment
        from rest_framework.pagination import PageNumberPagination
        customer = self.get_object()
        qs = (SalesInvoice.objects
              .filter(store=request.user.store, customer=customer, is_deleted=False)
              .order_by('-date'))

        class InvoiceSummarySerializer(drf_serializers.ModelSerializer):
            payment_method = drf_serializers.SerializerMethodField()
            class Meta:
                from finance.models import SalesInvoice as M
                model = M
                fields = ['id', 'invoice_number', 'status', 'date', 'grand_total', 'paid_amount', 'discount', 'payment_method', 'created_at']
            def get_payment_method(self, obj):
                p = Payment.objects.filter(invoice=obj).select_related('method').first()
                return p.method.name if p and p.method else None

        pager = PageNumberPagination()
        pager.page_size = 20
        page = pager.paginate_queryset(qs, request)
        return pager.get_paginated_response(InvoiceSummarySerializer(page, many=True).data)


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
        store = self.request.user.store
        if store is None:   # never list the storeless accounts (filter(store=None) would)
            return User.objects.none()
        return User.objects.filter(store=store, is_superadmin=False).order_by('first_name', 'username')

    # ── Rank guard (s156, §AUDIT B4) ────────────────────────────────────────
    # Nobody creates, promotes to, or edits an account at or above their own rank: an Admin can't make an
    # Owner/Admin, promote themselves, or deactivate / re-password the Owner. Own account: everyday fields
    # only — never your own role or active flag. Super-admins are exempt.
    def _check_rank(self, data, target=None):
        actor = self.request.user
        if getattr(actor, 'is_superadmin', False):
            return
        rank = _user_rank(actor)
        new_role = data.get('role')
        if target is not None and target.pk == actor.pk:
            if (new_role and new_role != actor.role) or data.get('is_active') is False:
                raise PermissionDenied("You can't change your own role or deactivate your own account.")
            return
        if target is not None and _user_rank(target) >= rank:
            raise PermissionDenied("You can't change an account with the same or a higher role than yours.")
        if new_role and (target is None or new_role != target.role) and ROLE_RANK.get(new_role, 99) >= rank:
            raise PermissionDenied("You can only give roles below your own.")

    def perform_create(self, serializer):
        from billing.quota import enforce_quota
        data = dict(serializer.validated_data)
        data.setdefault('role', User.Role.CASHIER)   # the model default — still rank-checked
        self._check_rank(data)
        enforce_quota(self.request.user.store, 'users')
        staff = serializer.save(store=self.request.user.store)
        log_activity(
            request=self.request,
            action=f"Added staff member: {staff.username}",
            op_type=ActivityLog.OperationType.STAFF,
            details={'staff_id': staff.id, 'username': staff.username, 'role': staff.role},
        )

    def perform_update(self, serializer):
        self._check_rank(serializer.validated_data, target=serializer.instance)
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
