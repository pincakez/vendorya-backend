from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import User


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


class VendoryaTokenObtainSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = UserProfileSerializer(self.user).data
        return data
