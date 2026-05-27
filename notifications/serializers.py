from rest_framework import serializers

from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    is_unread = serializers.BooleanField(read_only=True)

    class Meta:
        model  = Notification
        fields = ['id', 'type', 'title', 'body', 'link', 'payload',
                  'read_at', 'is_unread', 'created_at']
        read_only_fields = fields
