"""TOTP two-factor endpoints: enrol, confirm, status, disable, backup codes."""
import base64
from datetime import timedelta
from io import BytesIO

import qrcode
from django.db import transaction
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken

from .authentication import PreAuthJWTAuthentication, VendoryaJWTAuthentication
from .twofa import confirmed_totp_device, is_enrolled, requires_2fa

BACKUP_CODE_COUNT = 10


def make_pre_auth_token(user):
    """Short-lived token (10 min) that only unlocks the 2FA enrolment endpoints."""
    token = AccessToken.for_user(user)
    token['pre_auth'] = True
    token.set_exp(lifetime=timedelta(minutes=10))
    return str(token)


def _qr_data_uri(otpauth_url):
    img = qrcode.make(otpauth_url)
    buf = BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


def _issue_backup_codes(user):
    """Replace the user's static backup codes with a fresh set; return plaintext."""
    device, _ = StaticDevice.objects.get_or_create(user=user, name='backup')
    device.token_set.all().delete()
    codes = []
    for _ in range(BACKUP_CODE_COUNT):
        code = StaticToken.random_token()
        StaticToken.objects.create(device=device, token=code)
        codes.append(code)
    device.confirmed = True
    device.save()
    return codes


class TwoFactorSetupView(APIView):
    """Begin enrolment: create an unconfirmed TOTP device, return QR + secret URI.

    Accepts a pre-auth token so users forced to enrol can do so before login completes.
    """
    authentication_classes = [PreAuthJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if is_enrolled(user):
            return Response({'detail': '2FA is already enabled.'}, status=status.HTTP_400_BAD_REQUEST)
        # Reset any prior unconfirmed device so re-running enrolment is clean.
        TOTPDevice.objects.filter(user=user, confirmed=False).delete()
        device = TOTPDevice.objects.create(user=user, name='default', confirmed=False)
        return Response({
            'otpauth_url': device.config_url,
            'qr': _qr_data_uri(device.config_url),
        })


class TwoFactorVerifySetupView(APIView):
    """Confirm enrolment with a TOTP code; returns one-time backup codes."""
    authentication_classes = [PreAuthJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        token = str(request.data.get('token', '')).strip()
        device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
        if not device:
            return Response({'detail': 'No pending 2FA setup. Start setup first.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not device.verify_token(token):
            return Response({'detail': 'Invalid verification code.'},
                            status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            device.confirmed = True
            device.save()
            codes = _issue_backup_codes(user)
        return Response({'enabled': True, 'backup_codes': codes})


class TwoFactorStatusView(APIView):
    authentication_classes = [VendoryaJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            'enrolled': is_enrolled(request.user),
            'required': requires_2fa(request.user),
        })


class TwoFactorDisableView(APIView):
    """Disable 2FA for self. Requires current-password re-confirmation."""
    authentication_classes = [VendoryaJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        password = str(request.data.get('password', ''))
        if not user.check_password(password):
            return Response({'detail': 'Password is incorrect.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if requires_2fa(user):
            return Response({'detail': '2FA is mandatory for your role and cannot be disabled.'},
                            status=status.HTTP_403_FORBIDDEN)
        with transaction.atomic():
            TOTPDevice.objects.filter(user=user).delete()
            StaticDevice.objects.filter(user=user).delete()
        return Response({'enabled': False})


class TwoFactorBackupRegenerateView(APIView):
    """Regenerate backup codes (invalidates old ones). Requires being enrolled."""
    authentication_classes = [VendoryaJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not confirmed_totp_device(request.user):
            return Response({'detail': 'Enable 2FA before generating backup codes.'},
                            status=status.HTTP_400_BAD_REQUEST)
        codes = _issue_backup_codes(request.user)
        return Response({'backup_codes': codes})
