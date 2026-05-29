"""Two-factor (TOTP) helpers: requirement policy, device lookup, token matching.

Policy (item 4 + 8 of Auth Hardening):
  - super-admins (sudo) always require 2FA
  - OWNER always requires 2FA
  - role >= MANAGER requires 2FA when the store toggles force_2fa_managers on
"""
from django_otp import devices_for_user, match_token
from django_otp.plugins.otp_totp.models import TOTPDevice

from .permissions import ROLE_RANK
from .models import User


def requires_2fa(user):
    """True if this user must pass 2FA to log in."""
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'is_superadmin', False):
        return True
    if user.role == User.Role.OWNER:
        return True
    store = getattr(user, 'store', None)
    settings_obj = getattr(store, 'settings', None) if store else None
    if settings_obj and settings_obj.force_2fa_managers:
        return ROLE_RANK.get(user.role, 0) >= ROLE_RANK[User.Role.MANAGER]
    return False


def confirmed_totp_device(user):
    """The user's confirmed TOTP device, or None."""
    return TOTPDevice.objects.filter(user=user, confirmed=True).first()


def is_enrolled(user):
    """True if the user has a confirmed TOTP device."""
    return confirmed_totp_device(user) is not None


def verify_token(user, token):
    """Validate a 6-digit TOTP code or a static backup code. Returns the device or None.

    match_token walks all confirmed devices (TOTP + static backup), enforcing
    per-device throttling and burning a static backup code on use.
    """
    if not token:
        return None
    return match_token(user, str(token).strip())
