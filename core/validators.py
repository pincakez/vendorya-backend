"""Custom auth password validators for Vendorya."""
import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class ComplexityValidator:
    """Require a mix of character classes in passwords.

    Defaults: at least one uppercase letter, one lowercase letter, and one digit.
    Configured in AUTH_PASSWORD_VALIDATORS. Only runs on password set/change, so
    existing stored hashes are never re-validated.
    """

    def __init__(self, require_upper=True, require_lower=True, require_digit=True):
        self.require_upper = require_upper
        self.require_lower = require_lower
        self.require_digit = require_digit

    def validate(self, password, user=None):
        errors = []
        if self.require_upper and not re.search(r'[A-Z]', password):
            errors.append(_("at least one uppercase letter"))
        if self.require_lower and not re.search(r'[a-z]', password):
            errors.append(_("at least one lowercase letter"))
        if self.require_digit and not re.search(r'\d', password):
            errors.append(_("at least one digit"))
        if errors:
            raise ValidationError(
                _("Password must contain %(reqs)s.") % {'reqs': ", ".join(errors)},
                code='password_not_complex',
            )

    def get_help_text(self):
        reqs = []
        if self.require_upper:
            reqs.append(_("an uppercase letter"))
        if self.require_lower:
            reqs.append(_("a lowercase letter"))
        if self.require_digit:
            reqs.append(_("a digit"))
        return _("Your password must contain %(reqs)s.") % {'reqs': ", ".join(reqs)}
