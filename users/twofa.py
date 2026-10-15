"""Two-factor (TOTP) helpers: requirement policy, device lookup, token matching.

Policy (s174, Yakot: "not optional"): every shop **OWNER** must log in with an authenticator — the owner sees all
the money. Login gives an owner without one a short pre-auth token that only opens the setup screens, and setup
finishes the login. Everyone else stays opt-in (prompted only once they enrolled). Sudo has its own admin login.
A lost phone: backup codes, else sudo clears the device (admin_auth) and the next login asks to set it up again.
"""
from django_otp import devices_for_user, match_token
from django_otp.plugins.otp_totp.models import TOTPDevice

from .permissions import ROLE_RANK
from .models import User


def requires_2fa(user):
    """A shop owner must use an authenticator (s174). The single source of truth for the login view, the token refresh,
    the status and the disable endpoints."""
    return bool(user is not None and getattr(user, 'is_active', False)
                and getattr(user, 'role', None) == User.Role.OWNER and not getattr(user, 'is_superadmin', False))


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
