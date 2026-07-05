from rest_framework import serializers

from .models import APIKey
from .scopes import normalize_scopes, RESOURCE_GROUPS, ACCESS_LEVELS


class APIKeySerializer(serializers.ModelSerializer):
    """Read view — never exposes the secret (only the public prefix)."""
    store_name = serializers.CharField(source='store.name', read_only=True)

    class Meta:
        model = APIKey
        fields = [
            'id', 'label', 'key_prefix', 'scopes', 'is_active',
            'expires_at', 'last_used_at', 'created_at',
            'owner_name', 'max_rpd', 'max_rpm', 'penalty_minutes',
            'store_name',
        ]
        read_only_fields = fields


class APIKeyCreateSerializer(serializers.ModelSerializer):
    """Write view for minting a key. Returns the raw key exactly once."""
    scopes = serializers.ListField(child=serializers.CharField(), required=False, default=list)

    class Meta:
        model = APIKey
        fields = ['label', 'scopes', 'expires_at', 'owner_name', 'max_rpd', 'max_rpm', 'penalty_minutes']

    def validate_scopes(self, value):
        cleaned = normalize_scopes(value)
        if value and not cleaned:
            raise serializers.ValidationError("No valid scopes. Use '<group>:read' or '<group>:write'.")
        return cleaned

    def create(self, validated_data):
        request = self.context['request']
        obj, raw_key = APIKey.generate(
            store=request.user.store,
            created_by=request.user,
            label=validated_data['label'],
            scopes=validated_data.get('scopes', []),
            expires_at=validated_data.get('expires_at'),
        )
        obj.owner_name = validated_data.get('owner_name', '')
        obj.max_rpd = validated_data.get('max_rpd')
        obj.max_rpm = validated_data.get('max_rpm')
        obj.penalty_minutes = validated_data.get('penalty_minutes', 0)
        obj.save(update_fields=['owner_name', 'max_rpd', 'max_rpm', 'penalty_minutes', 'updated_at'])
        obj._raw_key = raw_key
        return obj


class AdminAPIKeySerializer(serializers.ModelSerializer):
    """Read view for sudo admin — includes store info."""
    store_id   = serializers.UUIDField(source='store.id', read_only=True)
    store_name = serializers.CharField(source='store.name', read_only=True)
    created_by_username = serializers.CharField(source='created_by.username', read_only=True, default=None)

    class Meta:
        model = APIKey
        fields = [
            'id', 'label', 'key_prefix', 'scopes', 'is_active',
            'expires_at', 'last_used_at', 'created_at', 'updated_at',
            'owner_name', 'max_rpd', 'max_rpm', 'penalty_minutes',
            'store_id', 'store_name', 'created_by_username',
        ]
        read_only_fields = ['id', 'key_prefix', 'created_at', 'updated_at',
                            'last_used_at', 'store_id', 'store_name', 'created_by_username']


class AdminAPIKeyCreateSerializer(serializers.ModelSerializer):
    """Admin create — must specify store_id."""
    store_id = serializers.UUIDField(write_only=True)
    scopes   = serializers.ListField(child=serializers.CharField(), required=False, default=list)

    class Meta:
        model = APIKey
        fields = ['store_id', 'label', 'scopes', 'expires_at',
                  'owner_name', 'max_rpd', 'max_rpm', 'penalty_minutes']

    def validate_scopes(self, value):
        cleaned = normalize_scopes(value)
        if value and not cleaned:
            raise serializers.ValidationError("No valid scopes.")
        return cleaned

    def create(self, validated_data):
        from core.models import Store
        store = Store.objects.get(pk=validated_data.pop('store_id'))
        request = self.context['request']
        obj, raw_key = APIKey.generate(
            store=store,
            created_by=request.user,
            label=validated_data['label'],
            scopes=validated_data.get('scopes', []),
            expires_at=validated_data.get('expires_at'),
        )
        obj.owner_name = validated_data.get('owner_name', '')
        obj.max_rpd = validated_data.get('max_rpd')
        obj.max_rpm = validated_data.get('max_rpm')
        obj.penalty_minutes = validated_data.get('penalty_minutes', 0)
        obj.save(update_fields=['owner_name', 'max_rpd', 'max_rpm', 'penalty_minutes', 'updated_at'])
        obj._raw_key = raw_key
        return obj
