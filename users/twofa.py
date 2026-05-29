"""Two-factor (TOTP) helpers: requirement policy, device lookup, token matching.

Policy: 2FA is **fully optional / opt-in**. No account is ever forced to enrol.
A user is only prompted for a code at login if they have voluntarily enrolled a
confirmed TOTP device (see the login view, which gates on ``is_enrolled``).
"""
from django_otp import devices_for_user, match_token
from django_otp.plugins.otp_totp.models import TOTPDevice

from .permissions import ROLE_RANK
from .models import User


def requires_2fa(user):
    """Forced 2FA is disabled — enrolment is always optional.

    Kept as a single source of truth so the login view, status, and disable
    endpoints all agree that nobody is *required* to use 2FA.
    """
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
