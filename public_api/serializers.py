from rest_framework import serializers

from .models import APIKey
from .scopes import normalize_scopes, RESOURCE_GROUPS, ACCESS_LEVELS


class APIKeySerializer(serializers.ModelSerializer):
    """Read view — never exposes the secret (only the public prefix)."""
    class Meta:
        model = APIKey
        fields = ['id', 'label', 'key_prefix', 'scopes', 'is_active',
                  'expires_at', 'last_used_at', 'created_at']
        read_only_fields = fields


class APIKeyCreateSerializer(serializers.ModelSerializer):
    """Write view for minting a key. Returns the raw key exactly once."""
    scopes = serializers.ListField(child=serializers.CharField(), required=False, default=list)

    class Meta:
        model = APIKey
        fields = ['label', 'scopes', 'expires_at']

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
        # Stash the one-time raw key so the view can surface it in the response.
        obj._raw_key = raw_key
        return obj
