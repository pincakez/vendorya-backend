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
                  'full_name', 'role', 'store', 'photo')

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name or obj.username


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ['id', 'name', 'phone_number', 'notes', 'balance']
        read_only_fields = ['id', 'balance']


class VendoryaTokenObtainSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = UserProfileSerializer(self.user).data
        return data
