from rest_framework import serializers
from .models import Service


class ServiceSerializer(serializers.ModelSerializer):
    client_display_name = serializers.SerializerMethodField()
    client_display_phone = serializers.SerializerMethodField()
    eta_label = serializers.SerializerMethodField()

    class Meta:
        model = Service
        fields = [
            'id', 'serial_number', 'store',
            'client', 'client_name', 'client_phone',
            'client_display_name', 'client_display_phone',
            'service_type', 'receive_date',
            'no_eta', 'eta_days', 'eta_hours', 'eta_datetime',
            'info', 'keeping', 'cost', 'diagnosis',
            'status', 'notify_bell', 'notified',
            'invoice', 'created_by',
            'created_at', 'updated_at',
            'eta_label',
        ]
        read_only_fields = [
            'id', 'serial_number', 'store', 'created_by',
            'invoice', 'eta_datetime', 'notified',
            'client_display_name', 'client_display_phone', 'eta_label',
        ]

    def get_client_display_name(self, obj):
        if obj.client_id:
            return obj.client.name
        return obj.client_name or ''

    def get_client_display_phone(self, obj):
        if obj.client_id:
            return obj.client.phone_number or ''
        return obj.client_phone or ''

    def get_eta_label(self, obj):
        if obj.no_eta or not obj.eta_datetime:
            return None
        from django.utils import timezone
        now = timezone.now()
        delta = obj.eta_datetime - now
        total_seconds = int(delta.total_seconds())
        if total_seconds <= 0:
            return 'overdue'
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def validate(self, data):
        no_eta = data.get('no_eta', getattr(self.instance, 'no_eta', True))
        if not no_eta:
            eta_days = data.get('eta_days', getattr(self.instance, 'eta_days', None))
            eta_hours = data.get('eta_hours', getattr(self.instance, 'eta_hours', None))
            if not eta_days and not eta_hours:
                raise serializers.ValidationError(
                    {'eta_days': 'Provide at least days or hours when ETA is enabled.'}
                )
        return data
