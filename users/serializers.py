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
                  'pos_settings', 'ui_prefs', 'phone_number', 'whatsapp_number')
        read_only_fields = ('is_superadmin', 'force_password_change', 'default_branch_name')

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name or obj.username


class CustomerSerializer(serializers.ModelSerializer):
    # Live balance = opening-balance seed + Σ posted-invoice AR. Computed, never
    # stored (the `balance` column holds only the opening seed). Positive = owes us.
    balance = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = ['id', 'name', 'phone_number', 'email', 'gender', 'notes',
                  'balance', 'credit_limit', 'store_credit', 'is_walk_in']
        read_only_fields = ['id', 'store_credit', 'is_walk_in']

    def get_balance(self, obj):
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
        user.set_password(password or User.objects.make_random_password())
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
