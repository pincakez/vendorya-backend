from rest_framework import serializers

from .models import SubscriptionPlan, Subscription, BillingInvoice, BillingSettings


# ---------- Platform billing settings (singleton) ----------

class BillingSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillingSettings
        fields = [
            'trial_length_days', 'grace_days', 'invoice_due_days',
            'quota_mode', 'nightly_job_enabled', 'last_run_at',
            'updated_at',
        ]
        read_only_fields = ['last_run_at', 'updated_at']


# ---------- Plans ----------

class SubscriptionPlanSerializer(serializers.ModelSerializer):
    active_subscriptions = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = SubscriptionPlan
        fields = [
            'id', 'name', 'description',
            'monthly_price', 'annual_price', 'currency',
            'max_users', 'max_branches', 'max_products', 'max_invoices_per_month',
            'is_active', 'active_subscriptions',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'active_subscriptions']

    def get_active_subscriptions(self, obj):
        return obj.subscriptions.exclude(status=Subscription.Status.CANCELLED).count()


# ---------- Subscriptions ----------

class _StoreNestedSerializer(serializers.Serializer):
    """Tiny embed for subscription rows in the admin list."""
    id   = serializers.UUIDField()
    name = serializers.CharField()
    owner_username = serializers.SerializerMethodField()

    def get_owner_username(self, store):
        return store.owner.username if store.owner_id else None


class AdminSubscriptionSerializer(serializers.ModelSerializer):
    """Used by sudo (`/api/admin/subscriptions/`). Lets sudo flip plan + status + custom_label."""
    store         = _StoreNestedSerializer(read_only=True)
    plan_name     = serializers.CharField(source='plan.name', read_only=True)
    display_label = serializers.CharField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'store', 'plan', 'plan_name', 'custom_label', 'display_label',
            'status', 'period_start', 'period_end', 'trial_ends_at',
            'cancelled_at', 'notes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'store', 'plan_name', 'display_label',
                            'cancelled_at', 'created_at', 'updated_at']


class TenantSubscriptionSerializer(serializers.ModelSerializer):
    """Read-only payload the store sees about its own subscription."""
    plan = SubscriptionPlanSerializer(read_only=True)
    display_label = serializers.CharField(read_only=True)

    class Meta:
        model  = Subscription
        fields = ['id', 'plan', 'display_label', 'status',
                  'period_start', 'period_end', 'trial_ends_at']


# ---------- Invoices ----------

class _SubscriptionNestedSerializer(serializers.Serializer):
    id            = serializers.UUIDField()
    plan_name     = serializers.SerializerMethodField()
    display_label = serializers.CharField()

    def get_plan_name(self, sub):
        return sub.plan.name


class BillingInvoiceSerializer(serializers.ModelSerializer):
    store_name   = serializers.CharField(source='store.name', read_only=True)
    subscription = _SubscriptionNestedSerializer(read_only=True)

    class Meta:
        model = BillingInvoice
        fields = [
            'id', 'invoice_number', 'status',
            'store', 'store_name', 'subscription',
            'amount', 'currency',
            'period_start', 'period_end',
            'issued_at', 'due_at', 'paid_at',
            'paid_method', 'paid_reference',
            'line_description', 'notes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'invoice_number', 'store_name', 'subscription',
                            'issued_at', 'paid_at', 'created_at', 'updated_at']


class AdminBillingInvoiceCreateSerializer(serializers.Serializer):
    """Sudo issues an invoice in a single call.  Always created as ISSUED so
    it lands in the tenant's inbox immediately."""
    store        = serializers.UUIDField()
    amount       = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency     = serializers.CharField(max_length=10, default='EGP')
    period_start = serializers.DateField(required=False, allow_null=True)
    period_end   = serializers.DateField(required=False, allow_null=True)
    due_at       = serializers.DateField(required=False, allow_null=True)
    line_description = serializers.CharField(max_length=255, required=False, allow_blank=True)
    notes        = serializers.CharField(required=False, allow_blank=True)
    issue        = serializers.BooleanField(default=True,
                                            help_text="When true, immediately moves DRAFT→ISSUED (default).")

    def validate_store(self, store_id):
        try:
            sub = Subscription.all_objects.select_related('store').get(store_id=store_id)  # sudo create, explicit store
        except Subscription.DoesNotExist:
            raise serializers.ValidationError("No subscription found for that store.")
        self.context['subscription'] = sub
        return store_id

    def create(self, validated_data):
        sub = self.context['subscription']
        do_issue = validated_data.pop('issue', True)
        invoice = BillingInvoice.objects.create(
            store=sub.store,
            subscription=sub,
            amount=validated_data['amount'],
            currency=validated_data.get('currency', 'EGP'),
            period_start=validated_data.get('period_start'),
            period_end=validated_data.get('period_end'),
            due_at=validated_data.get('due_at'),
            line_description=validated_data.get('line_description', ''),
            notes=validated_data.get('notes', ''),
            status=BillingInvoice.Status.DRAFT,
        )
        if do_issue:
            invoice.issue(by_user=self.context['request'].user)
        return invoice
