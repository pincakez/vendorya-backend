from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import User, Customer


class StoreMinimalSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    currency_symbol = serializers.CharField()
    plan = serializers.CharField()


class UserProfileSerializer(serializers.ModelSerializer):
    store = StoreMinimalSerializer(read_only=True)
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'first_name', 'last_name',
                  'full_name', 'role', 'store', 'photo', 'is_superadmin')
        read_only_fields = ('is_superadmin',)

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name or obj.username


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ['id', 'name', 'phone_number', 'notes', 'balance']
        read_only_fields = ['id', 'balance']


class StaffSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField(read_only=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name',
                  'full_name', 'role', 'is_active', 'photo', 'password']
        read_only_fields = ['id']

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name or obj.username

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
        data = super().validate(attrs)
        data['user'] = UserProfileSerializer(self.user).data
        return data
