from rest_framework import serializers
from .models import Store, Branch, Address
from users.models import User


class AdminStoreSerializer(serializers.ModelSerializer):
    owner_username = serializers.CharField(source='owner.username', read_only=True)
    branches_count = serializers.IntegerField(read_only=True)
    staff_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Store
        fields = [
            'id', 'name', 'owner', 'owner_username',
            'plan', 'is_active',
            'currency_symbol', 'default_language',
            'branches_count', 'staff_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


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
