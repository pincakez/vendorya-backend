from rest_framework import serializers

from .models import (
    AISettings, AIProfile, AIModelCache,
    AIConversation, AIMessage, AIKnowledgeChunk,
)


class AISettingsSerializer(serializers.ModelSerializer):
    has_key       = serializers.BooleanField(read_only=True)
    masked_key    = serializers.SerializerMethodField()
    gemini_api_key = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model  = AISettings
        fields = ['has_key', 'masked_key', 'gemini_api_key',
                  'extra_models', 'hidden_models', 'updated_at']
        read_only_fields = ['updated_at']

    def get_masked_key(self, obj):
        key = obj.gemini_api_key or ''
        if not key:
            return ''
        return f"••••{key[-4:]}" if len(key) >= 4 else '••••'


class AIModelCacheSerializer(serializers.ModelSerializer):
    class Meta:
        model  = AIModelCache
        fields = [
            'id', 'model_id', 'display_name', 'description',
            'rpm', 'rpd', 'tokens',
            'supports_thinking', 'supports_grounding', 'supports_vision', 'supports_audio',
            'last_refreshed_at',
        ]
        read_only_fields = fields


class AIProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model  = AIProfile
        fields = [
            'id', 'name', 'avatar', 'is_active', 'global_knowledge',
            'model_id', 'vision_resolution', 'max_output_tokens', 'thinking_level',
            'top_p', 'top_k', 'temperature', 'google_grounding',
            'system_instruction', 'enabled_tools',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, attrs):
        # Cap profile count at 3 (per TODO C4 spec).
        if self.instance is None:
            existing = AIProfile.objects.count()
            if existing >= 3:
                raise serializers.ValidationError(
                    "Maximum of 3 AI profiles allowed. Delete one before creating another."
                )
        if attrs.get('temperature') is not None and not (0.0 <= attrs['temperature'] <= 2.0):
            raise serializers.ValidationError({'temperature': 'Must be between 0.0 and 2.0.'})
        if attrs.get('top_p') is not None and not (0.0 <= attrs['top_p'] <= 1.0):
            raise serializers.ValidationError({'top_p': 'Must be between 0.0 and 1.0.'})
        return attrs


class AIMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model  = AIMessage
        fields = ['id', 'role', 'content', 'attachments', 'tool_calls', 'usage', 'created_at']
        read_only_fields = fields


class AIConversationSerializer(serializers.ModelSerializer):
    message_count = serializers.SerializerMethodField()
    first_message = serializers.SerializerMethodField()

    class Meta:
        model  = AIConversation
        fields = ['id', 'title', 'acting_store', 'profile', 'message_count',
                  'first_message', 'created_at', 'updated_at']
        read_only_fields = fields

    def get_message_count(self, obj):
        return obj.messages.count()

    def get_first_message(self, obj):
        msg = obj.messages.filter(role=AIMessage.Role.USER).order_by('created_at').first()
        return (msg.content or '')[:120] if msg else ''


class AIKnowledgeChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model  = AIKnowledgeChunk
        fields = ['id', 'source_name', 'source_type', 'chunk_index',
                  'content', 'industries', 'metadata', 'created_at']
        read_only_fields = ['created_at']
