from django.utils.crypto import get_random_string
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import User, Customer


class _CurrencyNestedSerializer(serializers.Serializer):
    """Minimal currency embed for the login payload."""
    id       = serializers.UUIDField()
    code     = serializers.CharField()
    symbol   = serializers.CharField()
    position = serializers.CharField()


class StoreMinimalSerializer(serializers.Serializer):
    id            = serializers.UUIDField()
    name          = serializers.CharField()
    plan          = serializers.CharField()
    timezone      = serializers.CharField()
    default_language = serializers.CharField()   # s167: receipts print in the shop's language
    currency      = _CurrencyNestedSerializer(allow_null=True)
    logo_light_url = serializers.SerializerMethodField()
    logo_dark_url  = serializers.SerializerMethodField()

    def _abs(self, obj, field):
        f = getattr(obj, field, None)
        if not f:
            return None
        req = self.context.get('request')
        return req.build_absolute_uri(f.url) if req else f.url

    def get_logo_light_url(self, obj): return self._abs(obj, 'logo_light')
    def get_logo_dark_url(self,  obj): return self._abs(obj, 'logo_dark')


class UserProfileSerializer(serializers.ModelSerializer):
    store = StoreMinimalSerializer(read_only=True)
    full_name = serializers.SerializerMethodField()
    default_branch_name = serializers.CharField(source='default_branch.name', read_only=True, allow_null=True)

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'first_name', 'last_name',
                  'full_name', 'role', 'store', 'photo', 'is_superadmin',
                  'force_password_change', 'default_branch', 'default_branch_name',
                  'pos_settings', 'ui_prefs', 'phone_number', 'whatsapp_number',
                  'language')
        read_only_fields = ('is_superadmin', 'force_password_change', 'default_branch_name')

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name or obj.username


class CustomerSerializer(serializers.ModelSerializer):
    # Live balance = opening-balance seed + Σ posted-invoice AR. Computed, never
    # stored (the `balance` column holds only the opening seed). Positive = owes us.
    balance = serializers.SerializerMethodField()
    # Opening balance = what they owed before Vendorya (s167, PROLINE-PARITY Upgrade 4) — the `balance` seed
    # column. Negative = we owed them. MANAGER+ only: a cashier could otherwise wipe a debt.
    opening_balance = serializers.DecimalField(source='balance', max_digits=12, decimal_places=2, required=False)

    class Meta:
        model = Customer
        fields = ['id', 'name', 'phone_number', 'email', 'gender', 'notes',
                  'whatsapp_number', 'instagram', 'website', 'country', 'city',
                  'balance', 'opening_balance', 'credit_limit', 'store_credit', 'is_walk_in']
        read_only_fields = ['id', 'store_credit', 'is_walk_in']

    def validate_opening_balance(self, value):
        from decimal import Decimal
        from rest_framework.exceptions import PermissionDenied
        from .permissions import ROLE_RANK, _user_rank
        request = self.context.get('request')
        current = self.instance.balance if self.instance else Decimal('0')
        if request and value != current and _user_rank(request.user) < ROLE_RANK[User.Role.MANAGER]:
            raise PermissionDenied("Only a manager can set a customer's opening balance.")
        return value

    def validate_credit_limit(self, value):
        """How much this customer may owe — MANAGER+ only (Yakot, 2026-10-14), same rule as the opening
        balance: a cashier could otherwise raise a friend's limit and sell them more on credit.
        Sending the unchanged value (a full form) is fine; leaving it out keeps it."""
        from rest_framework.exceptions import PermissionDenied
        from .permissions import ROLE_RANK, _user_rank
        request = self.context.get('request')
        current = self.instance.credit_limit if self.instance else None
        if request and value != current and _user_rank(request.user) < ROLE_RANK[User.Role.MANAGER]:
            raise PermissionDenied("Only a manager can set a customer's credit limit.")
        return value

    def get_balance(self, obj):
        # The list annotates `live_balance` in one query (users.views.with_live_balance, §A36);
        # anything else (create/update responses, other callers) falls back to the per-customer query.
        live = getattr(obj, 'live_balance', None)
        if live is not None:
            return str(live)
        from finance.models import customer_outstanding
        return str(customer_outstanding(obj))


class StaffSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField(read_only=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name',
                  'full_name', 'role', 'is_active', 'photo', 'password',
                  'phone_number', 'whatsapp_number']
        read_only_fields = ['id']

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name or obj.username

    def validate_password(self, value):
        # Empty = "keep current / auto-generate"; only validate a real password.
        if value:
            try:
                validate_password(value)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(list(exc.messages))
        return value

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        user = User(**validated_data)
        # make_random_password() was removed in Django 5.1 — it crashed every password-less add (s156).
        user.set_password(password or get_random_string(20))
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class VendoryaTokenObtainSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        # BUG-001: the FE field is labeled "Username", but users naturally type
        # their email. If the value looks like an email and maps to exactly one
        # active user, resolve it to that user's username before authenticating.
        # Ambiguous (duplicate emails) or no-match cases fall through untouched
        # so the normal "invalid credentials" path still applies.
        login = attrs.get(self.username_field, '')
        if login and '@' in login:
            matches = User.objects.filter(email__iexact=login, is_active=True)
            if matches.count() == 1:
                attrs[self.username_field] = matches.first().username

        data = super().validate(attrs)

        # Block login when the user's store has been suspended (billing past-due
        # auto-suspend or a manual sudo suspend). Sudo users (store=None) are exempt.
        store = getattr(self.user, 'store', None)
        if store is not None and not store.is_active:
            from rest_framework import exceptions
            raise exceptions.AuthenticationFailed(
                "This store has been suspended. Please contact support.",
                code='store_suspended',
            )

        data['user'] = UserProfileSerializer(self.user).data
        return data
