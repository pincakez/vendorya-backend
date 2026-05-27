from rest_framework import serializers
from .models import Store, Branch, Address, StoreSettings, ActivityLog


class StoreSerializer(serializers.ModelSerializer):
    class Meta:
        model = Store
        fields = ['id', 'name', 'currency_symbol', 'default_language', 'plan', 'is_active']
        read_only_fields = ['id', 'plan', 'is_active']


class BranchSerializer(serializers.ModelSerializer):
    # Flattened address fields
    street_1 = serializers.CharField(write_only=True, required=False, allow_blank=True, default='')
    city     = serializers.CharField(write_only=True, required=False, allow_blank=True, default='')
    country  = serializers.CharField(write_only=True, required=False, allow_blank=True, default='Egypt')

    address_street_1 = serializers.CharField(source='address.street_1', read_only=True)
    address_city     = serializers.CharField(source='address.city',     read_only=True)
    address_country  = serializers.CharField(source='address.country',  read_only=True)

    class Meta:
        model = Branch
        fields = ['id', 'name', 'is_main_branch',
                  'street_1', 'city', 'country',
                  'address_street_1', 'address_city', 'address_country']
        read_only_fields = ['id']

    def create(self, validated_data):
        street_1 = validated_data.pop('street_1', '')
        city     = validated_data.pop('city', '')
        country  = validated_data.pop('country', 'Egypt')
        store    = validated_data['store']
        address  = Address.objects.create(store=store, street_1=street_1 or '-', city=city or '-', country=country)
        return Branch.objects.create(address=address, **validated_data)

    def update(self, instance, validated_data):
        street_1 = validated_data.pop('street_1', None)
        city     = validated_data.pop('city', None)
        country  = validated_data.pop('country', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if any(v is not None for v in (street_1, city, country)):
            addr = instance.address
            if street_1 is not None: addr.street_1 = street_1 or '-'
            if city     is not None: addr.city     = city     or '-'
            if country  is not None: addr.country  = country
            addr.save()
        return instance


class StoreSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = StoreSettings
        fields = [
            'allow_negative_stock', 'enable_agel_selling',
            'tax_id', 'commercial_reg',
            'receipt_header', 'receipt_footer',
            'default_tax',
        ]


class ActivityLogSerializer(serializers.ModelSerializer):
    username  = serializers.CharField(source='user.username', read_only=True)
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = ActivityLog
        fields = ['id', 'username', 'full_name',
                  'operation_type', 'action', 'details',
                  'ip_address', 'timestamp']

    def get_full_name(self, obj):
        if not obj.user:
            return None
        name = f"{obj.user.first_name} {obj.user.last_name}".strip()
        return name or obj.user.username


class AdminActivityLogSerializer(ActivityLogSerializer):
    """Same as ActivityLog but also exposes the store (for the sudo global view)."""
    store_name = serializers.CharField(source='store.name', read_only=True)

    class Meta(ActivityLogSerializer.Meta):
        fields = ActivityLogSerializer.Meta.fields + ['store_name']
