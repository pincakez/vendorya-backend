from django.db import transaction
from rest_framework import serializers
from .models import Store, Branch, Address, Currency
from .serializers import _CurrencyNestedSerializer
from users.models import User


class AdminStoreSerializer(serializers.ModelSerializer):
    owner_username = serializers.CharField(source='owner.username', read_only=True)
    branches_count = serializers.IntegerField(read_only=True)
    staff_count    = serializers.IntegerField(read_only=True)
    currency       = _CurrencyNestedSerializer(read_only=True)
    currency_id    = serializers.PrimaryKeyRelatedField(
        source='currency', queryset=Currency.objects.filter(is_active=True),
        write_only=True, required=False,
    )

    class Meta:
        model = Store
        fields = [
            'id', 'name', 'owner', 'owner_username',
            'plan', 'is_active',
            'currency', 'currency_id',
            'default_language', 'timezone',
            'branches_count', 'staff_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class _OwnerInputSerializer(serializers.Serializer):
    username   = serializers.CharField(max_length=150)
    password   = serializers.CharField(max_length=128, min_length=8)
    email      = serializers.EmailField(required=False, allow_blank=True)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name  = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError("Username is already taken.")
        return value


class _StoreInputSerializer(serializers.Serializer):
    name             = serializers.CharField(max_length=200)
    plan             = serializers.ChoiceField(choices=Store.SubscriptionPlan.choices, default=Store.SubscriptionPlan.FREE)
    currency         = serializers.PrimaryKeyRelatedField(
        queryset=Currency.objects.filter(is_active=True),
        required=False, allow_null=True,
    )
    default_language = serializers.CharField(max_length=5, default='ar')
    timezone         = serializers.CharField(max_length=64, default='Africa/Cairo')


class _BranchInputSerializer(serializers.Serializer):
    name     = serializers.CharField(max_length=150, default='Main Branch')
    street_1 = serializers.CharField(max_length=255)
    street_2 = serializers.CharField(max_length=255, required=False, allow_blank=True)
    city     = serializers.CharField(max_length=100)
    country  = serializers.CharField(max_length=100, default='Egypt')


class AdminStoreCreateSerializer(serializers.Serializer):
    """Compound payload for sudo-driven tenant onboarding.

    Atomically creates: owner User → Store → main Branch (with Address).
    StoreSettings is auto-created by the post_save signal on Store.
    """
    owner  = _OwnerInputSerializer()
    store  = _StoreInputSerializer()
    branch = _BranchInputSerializer()

    @transaction.atomic
    def create(self, validated_data):
        owner_data  = validated_data['owner']
        store_data  = validated_data['store']
        branch_data = validated_data['branch']

        # 1. Owner user, no store FK yet (Store needs an owner; User can exist storeless)
        password = owner_data.pop('password')
        owner = User(
            role=User.Role.OWNER,
            is_active=True,
            is_superadmin=False,
            **owner_data,
        )
        owner.set_password(password)
        owner.save()

        # Default currency to EGP if sudo didn't pick one.
        if not store_data.get('currency'):
            store_data['currency'] = (Currency.objects.filter(code='EGP').first()
                                      or Currency.objects.filter(is_active=True).first())

        # 2. Store — owner is now bound, triggers post_save signal → StoreSettings
        store = Store.objects.create(owner=owner, **store_data)

        # 3. Backfill owner.store
        owner.store = store
        owner.save(update_fields=['store'])

        # 4. Main branch address + branch (SalesInvoice.branch is on_delete=PROTECT,
        #    so every store needs at least one branch from day one).
        address = Address.objects.create(
            store=store,
            street_1=branch_data['street_1'],
            street_2=branch_data.get('street_2') or None,
            city=branch_data['city'],
            country=branch_data['country'],
        )
        Branch.objects.create(
            store=store,
            address=address,
            name=branch_data['name'],
            is_main_branch=True,
        )

        return store


class AdminBranchSerializer(serializers.ModelSerializer):
    store_name = serializers.CharField(source='store.name', read_only=True)
    street_1 = serializers.CharField(source='address.street_1', read_only=True)
    city = serializers.CharField(source='address.city', read_only=True)
    country = serializers.CharField(source='address.country', read_only=True)

    class Meta:
        model = Branch
        fields = ['id', 'name', 'store', 'store_name', 'is_main_branch',
                  'street_1', 'city', 'country']
        read_only_fields = ['id']


class AdminUserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField(read_only=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name',
                  'full_name', 'is_superadmin', 'is_active', 'password']
        read_only_fields = ['id']

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name or obj.username

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        validated_data['is_superadmin'] = True
        validated_data['store'] = None
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
