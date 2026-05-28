from django.contrib import admin

from .models import (
    AISettings, AIProfile, AIModelCache,
    AIConversation, AIMessage, AIKnowledgeChunk,
)


@admin.register(AISettings)
class AISettingsAdmin(admin.ModelAdmin):
    list_display = ['id', 'has_key', 'updated_at']
    readonly_fields = ['updated_at', 'created_at']


@admin.register(AIProfile)
class AIProfileAdmin(admin.ModelAdmin):
    list_display = ['name', 'is_active', 'model_id', 'temperature', 'updated_at']
    list_filter = ['is_active', 'thinking_level']
    search_fields = ['name', 'model_id']


@admin.register(AIModelCache)
class AIModelCacheAdmin(admin.ModelAdmin):
    list_display = ['model_id', 'display_name', 'rpm', 'rpd', 'tokens', 'last_refreshed_at']
    search_fields = ['model_id', 'display_name']


@admin.register(AIConversation)
class AIConversationAdmin(admin.ModelAdmin):
    list_display = ['title', 'user', 'acting_store', 'profile', 'updated_at']
    list_filter = ['profile']
    search_fields = ['title', 'user__username']


@admin.register(AIMessage)
class AIMessageAdmin(admin.ModelAdmin):
    list_display = ['conversation', 'role', 'created_at']
    list_filter = ['role']


@admin.register(AIKnowledgeChunk)
class AIKnowledgeChunkAdmin(admin.ModelAdmin):
    list_display = ['source_name', 'chunk_index', 'industries', 'created_at']
    search_fields = ['source_name', 'content']
