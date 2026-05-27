from decimal import Decimal
from rest_framework import serializers
from django.db import transaction
from .models import (
    SalesInvoice, SalesInvoiceItem, Payment, PaymentMethod,
    PurchaseInvoice, PurchaseItem,
    Expense, ExpenseCategory,
    WorkShift,
    RefundInvoice, RefundItem,
)


class PaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentMethod
        fields = ['id', 'name', 'is_cash']
        read_only_fields = ['id']


# --- SALES ---

class SalesInvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalesInvoiceItem
        fields = ['id', 'variant', 'quantity', 'unit_price', 'tax_amount', 'total']
        read_only_fields = ['id', 'total']


class SalesInvoiceSerializer(serializers.ModelSerializer):
    items = SalesInvoiceItemSerializer(many=True, required=False)

    class Meta:
        model = SalesInvoice
        fields = [
            'id', 'branch', 'customer', 'invoice_number', 'status', 'date',
            'subtotal', 'tax_total', 'discount', 'grand_total', 'paid_amount',
            'items', 'created_at',
        ]
        read_only_fields = [
            'id', 'invoice_number', 'subtotal', 'tax_total',
            'grand_total', 'paid_amount', 'created_at',
        ]

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        with transaction.atomic():
            invoice = SalesInvoice.objects.create(**validated_data)
            for item_data in items_data:
                SalesInvoiceItem.objects.create(invoice=invoice, **item_data)
            self._recalculate(invoice)
        return invoice

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()
            if items_data is not None:
                instance.items.all().delete()
                for item_data in items_data:
                    SalesInvoiceItem.objects.create(invoice=instance, **item_data)
            self._recalculate(instance)
        return instance

    @staticmethod
    def _recalculate(invoice):
        subtotal = Decimal('0')
        tax_total = Decimal('0')
        for item in invoice.items.all():
            subtotal += item.quantity * item.unit_price
            tax_total += item.tax_amount
        invoice.subtotal = subtotal
        invoice.tax_total = tax_total
        invoice.grand_total = subtotal + tax_total - invoice.discount
        invoice.save(update_fields=['subtotal', 'tax_total', 'grand_total'])


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ['id', 'invoice', 'method', 'amount', 'created_by', 'created_at']
        read_only_fields = ['id', 'created_by', 'created_at']


# --- PURCHASE ---

class PurchaseItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseItem
        fields = ['id', 'variant', 'quantity', 'unit_cost', 'total_cost']
        read_only_fields = ['id', 'total_cost']


class PurchaseInvoiceSerializer(serializers.ModelSerializer):
    items = PurchaseItemSerializer(many=True, required=False)

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'supplier', 'branch', 'vendor_reference', 'date', 'status',
            'total_amount', 'paid_amount', 'notes', 'items', 'created_at',
        ]
        read_only_fields = ['id', 'total_amount', 'created_at']

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        with transaction.atomic():
            invoice = PurchaseInvoice.objects.create(**validated_data)
            for item_data in items_data:
                PurchaseItem.objects.create(invoice=invoice, **item_data)
        return invoice

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()
            if items_data is not None:
                if instance.status == PurchaseInvoice.Status.RECEIVED:
                    raise serializers.ValidationError(
                        'Cannot modify items on a received purchase.'
                    )
                instance.items.all().delete()
                for item_data in items_data:
                    PurchaseItem.objects.create(invoice=instance, **item_data)
        return instance


# --- EXPENSES ---

class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ['id', 'name', 'parent']
        read_only_fields = ['id']


class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = ['id', 'branch', 'category', 'amount', 'description', 'date', 'created_at']
        read_only_fields = ['id', 'created_at']


# --- SHIFTS ---

class WorkShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkShift
        fields = [
            'id', 'branch', 'user', 'start_time', 'end_time', 'status',
            'starting_cash', 'closing_cash', 'expected_cash', 'difference',
        ]
        read_only_fields = [
            'id', 'user', 'start_time', 'end_time', 'status',
            'expected_cash', 'difference', 'closing_cash',
        ]


# --- REFUNDS ---

class RefundItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = RefundItem
        fields = ['id', 'variant', 'quantity', 'refund_amount', 'restock_inventory']
        read_only_fields = ['id']


class RefundInvoiceSerializer(serializers.ModelSerializer):
    items = RefundItemSerializer(many=True, required=False)

    class Meta:
        model = RefundInvoice
        fields = [
            'id', 'branch', 'original_invoice', 'customer', 'refund_number',
            'date', 'total_refunded', 'reason', 'items',
        ]
        read_only_fields = ['id', 'refund_number', 'date', 'total_refunded']

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        with transaction.atomic():
            refund = RefundInvoice.objects.create(**validated_data)
            for item_data in items_data:
                RefundItem.objects.create(refund=refund, **item_data)
        return refund
