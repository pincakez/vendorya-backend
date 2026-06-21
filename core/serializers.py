from rest_framework import serializers
from .models import Store, Branch, Address, StoreSettings, ActivityLog, Currency, LabelPreset


class CurrencySerializer(serializers.ModelSerializer):
    class Meta:
        model = Currency
        fields = ['id', 'code', 'symbol', 'name', 'position', 'is_active',
                  'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class _CurrencyNestedSerializer(serializers.ModelSerializer):
    """Embed used on Store payloads — keeps the frontend formatter self-sufficient."""
    class Meta:
        model = Currency
        fields = ['id', 'code', 'symbol', 'position']


class StoreSerializer(serializers.ModelSerializer):
    currency = _CurrencyNestedSerializer(read_only=True)
    currency_id = serializers.PrimaryKeyRelatedField(
        source='currency', queryset=Currency.objects.filter(is_active=True),
        write_only=True, required=False,
    )
    logo_light_url = serializers.SerializerMethodField()
    logo_dark_url  = serializers.SerializerMethodField()

    def _abs(self, obj, field):
        f = getattr(obj, field)
        if not f:
            return None
        req = self.context.get('request')
        return req.build_absolute_uri(f.url) if req else f.url

    def get_logo_light_url(self, obj): return self._abs(obj, 'logo_light')
    def get_logo_dark_url(self,  obj): return self._abs(obj, 'logo_dark')

    class Meta:
        model = Store
        fields = ['id', 'name', 'store_code', 'store_type', 'currency', 'currency_id',
                  'default_language', 'timezone', 'plan', 'is_active',
                  'phone_number', 'whatsapp_number', 'city', 'country',
                  'address_line', 'email', 'website', 'fb_page', 'instagram',
                  'logo_light_url', 'logo_dark_url']
        read_only_fields = ['id', 'plan', 'is_active', 'store_code', 'store_type',
                            'logo_light_url', 'logo_dark_url']


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
                  'address_street_1', 'address_city', 'address_country',
                  'phone_number', 'email']
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
            'expiry_tracking_enabled', 'expired_sale_policy', 'expiry_alert_days',
            'multi_unit_enabled', 'weight_selling_enabled',
            'credit_policy', 'default_credit_limit',
            'return_window_days', 'restocking_fee_percent',
            'decimals', 'thousands_separator', 'item_noun', 'category_level_names',
            'base_unit_name', 'unit_tier_names',
            'tax_id', 'print_tax_id', 'commercial_reg',
            'receipt_header', 'receipt_footer',
            'default_tax', 'tax_enabled',
            'product_numbering_mode',
            'session_timeout_minutes', 'login_ip_allowlist', 'force_2fa_managers',
            'service_types', 'service_notify_hours',
            'label_printer_name', 'receipt_printer_name',
            'pos_print_default', 'pos_double_print_default',
            'srv_print_default', 'srv_double_print_default',
            'receipt_copies', 'receipt_auto_cut', 'receipt_cut_feed',
            'field_visibility',
            'pos_top_selling_period', 'pos_top_selling_category', 'pos_top_selling_limit',
            'pos_cart_display_fields',
            'default_info_sound', 'default_warning_sound', 'default_alert_sound',
        ]

    def validate_pos_cart_display_fields(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of field tokens.")
        # Dedupe, drop blanks, cap at 4 — these render as sub-labels on each cart line.
        out = []
        for tok in value:
            tok = str(tok).strip()
            if tok and tok not in out:
                out.append(tok)
        if len(out) > 4:
            raise serializers.ValidationError("Choose at most 4 fields.")
        return out

    def validate_decimals(self, value):
        if value < 0 or value > 4:
            raise serializers.ValidationError("Decimals must be between 0 and 4.")
        return value

    def validate_category_level_names(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of names.")
        defaults = ['Category', 'Sub-category', 'Sub-category 2', 'Sub-category 3']
        # Normalize to exactly 4 non-empty names, falling back to defaults.
        out = []
        for i in range(4):
            name = (str(value[i]).strip() if i < len(value) and value[i] else '')
            out.append(name or defaults[i])
        return out

    def validate_unit_tier_names(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of names.")
        defaults = ['Strip', 'Pack']
        # Normalize to exactly 2 non-empty names (the two tiers above the base unit).
        out = []
        for i in range(2):
            name = (str(value[i]).strip() if i < len(value) and value[i] else '')
            out.append(name or defaults[i])
        return out

    def validate_session_timeout_minutes(self, value):
        if value < 0 or value > 1440:
            raise serializers.ValidationError("Session timeout must be between 0 and 1440 minutes.")
        return value

    def validate_login_ip_allowlist(self, value):
        from core.security import validate_allowlist
        try:
            validate_allowlist(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))
        return value


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


class LabelPresetSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabelPreset
        fields = [
            'id', 'name', 'width_mm', 'height_mm',
            'show_store_name', 'show_product_name', 'show_sku', 'show_barcode', 'show_price',
            'is_default', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']
