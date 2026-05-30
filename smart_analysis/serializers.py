from rest_framework import serializers
from .models import TablePreset


class TablePresetSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = TablePreset
        fields = ['id', 'table_id', 'name', 'config', 'is_default', 'created_by_name', 'updated_at']
        read_only_fields = ['id', 'created_by_name', 'updated_at']
