from rest_framework import serializers

from .models import Notification, NotificationPreference


class NotificationSerializer(serializers.ModelSerializer):
    is_unread = serializers.BooleanField(read_only=True)

    class Meta:
        model  = Notification
        fields = ['id', 'priority', 'type', 'title', 'body', 'link', 'payload',
                  'read_at', 'is_unread', 'created_at']
        read_only_fields = fields


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model  = NotificationPreference
        fields = [
            'info_enabled', 'warning_enabled', 'alert_enabled',
            'info_sound', 'warning_sound', 'alert_sound', 'admin_sound',
        ]
