"""Super-admin Auth Settings API — search any user and perform recovery actions.

Sudo-only. Powers the /admin/auth-settings page:
  - search across ALL users (store staff + super-admins)
  - renew password (set a temp password; user must change it on next login)
  - disable 2FA / clear 2FA tokens for a user
"""
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.activity import log_activity
from core.models import ActivityLog
from .models import User
from .permissions import IsSuperAdmin
from .twofa import is_enrolled


def _user_card(user):
    return {
        'id': user.id,
        'username': user.username,
        'full_name': f"{user.first_name} {user.last_name}".strip() or user.username,
        'email': user.email,
        'role': user.role,
        'is_superadmin': user.is_superadmin,
        'is_active': user.is_active,
        'store_name': user.store.name if user.store_id else None,
        'has_2fa': is_enrolled(user),
        'force_password_change': user.force_password_change,
    }


class AdminUserSearchView(APIView):
    """GET ?q= — autocomplete across all users (staff + super-admins)."""
    permission_classes = [IsSuperAdmin]

    def get(self, request):
        q = (request.query_params.get('q') or '').strip()
        qs = User.objects.select_related('store').order_by('username')
        if q:
            qs = qs.filter(
                Q(username__icontains=q) | Q(first_name__icontains=q) |
                Q(last_name__icontains=q) | Q(email__icontains=q)
            )
        return Response([_user_card(u) for u in qs[:20]])


class AdminUserDetailView(APIView):
    permission_classes = [IsSuperAdmin]

    def get(self, request, user_id):
        user = User.objects.select_related('store').filter(pk=user_id).first()
        if not user:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_user_card(user))


class AdminRenewPasswordView(APIView):
    """Set a temp password for a user; they must change it on next login."""
    permission_classes = [IsSuperAdmin]

    def post(self, request, user_id):
        user = User.objects.filter(pk=user_id).first()
        if not user:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
        temp = str(request.data.get('temp_password', '')).strip()
        if not temp:
            return Response({'detail': 'Temp password is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_password(temp, user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'temp_password': list(exc.messages)})
        with transaction.atomic():
            user.set_password(temp)
            user.force_password_change = True
            user.save(update_fields=['password', 'force_password_change'])
        log_activity(
            request=request,
            action=f"Renewed password for user: {user.username}",
            op_type=ActivityLog.OperationType.STAFF,
            details={'target_user_id': str(user.id), 'username': user.username},
        )
        return Response({'detail': f'Temp password set for {user.username}. They must change it on next login.'})


class AdminDisable2FAView(APIView):
    """Turn 2FA off for a user (removes their confirmed TOTP device)."""
    permission_classes = [IsSuperAdmin]

    def post(self, request, user_id):
        user = User.objects.filter(pk=user_id).first()
        if not user:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            TOTPDevice.objects.filter(user=user).delete()
        log_activity(
            request=request,
            action=f"Disabled 2FA for user: {user.username}",
            op_type=ActivityLog.OperationType.STAFF,
            details={'target_user_id': str(user.id), 'username': user.username},
        )
        return Response({'detail': f'2FA disabled for {user.username}.', 'has_2fa': False})


class AdminClear2FATokensView(APIView):
    """Full 2FA wipe: TOTP device + static backup codes."""
    permission_classes = [IsSuperAdmin]

    def post(self, request, user_id):
        user = User.objects.filter(pk=user_id).first()
        if not user:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            TOTPDevice.objects.filter(user=user).delete()
            StaticDevice.objects.filter(user=user).delete()
        log_activity(
            request=request,
            action=f"Cleared all 2FA tokens for user: {user.username}",
            op_type=ActivityLog.OperationType.STAFF,
            details={'target_user_id': str(user.id), 'username': user.username},
        )
        return Response({'detail': f'2FA tokens cleared for {user.username}.', 'has_2fa': False})
