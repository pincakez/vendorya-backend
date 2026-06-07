from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.models import Store, StoreSettings, Branch, Address
from users.models import User, Customer
from inventory.models import Supplier, Product, ProductVariant, StockLevel
from finance.models import SalesInvoice, SalesInvoiceItem
from finance.serializers import RefundInvoiceSerializer


class ReturnsPolicyTests(TestCase):
    """Return-window enforcement, restocking fee, and store-credit accrual (E2)."""

    def setUp(self):
        self.owner = User.objects.create_user(username='owner_r', password='x')
        self.store = Store.objects.create(name='S1', store_code='100', owner=self.owner)
        self.settings, _ = StoreSettings.objects.get_or_create(store=self.store)
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        self.supplier = Supplier.objects.create(
            store=self.store, name='Sup', code_prefix='400', prefix_locked=True)
        product = Product.objects.create(store=self.store, name='Laptop', supplier=self.supplier)
        self.variant = ProductVariant.objects.create(product=product, sell_price=Decimal('100'))
        StockLevel.objects.create(variant=self.variant, branch=self.branch, quantity=Decimal('10'))
        self.customer = Customer.objects.create(
            store=self.store, name='Buyer', phone_number='0100')

    def _make_invoice(self, when=None):
        inv = SalesInvoice.objects.create(
            store=self.store, branch=self.branch, customer=self.customer,
            status=SalesInvoice.Status.POSTED, date=timezone.now(),
        )
        SalesInvoiceItem.objects.create(
            invoice=inv, variant=self.variant, quantity=Decimal('2'),
            unit_price=Decimal('100'),
        )
        if when is not None:
            SalesInvoice.objects.filter(pk=inv.pk).update(date=when)
            inv.refresh_from_db()
        return inv

    def _refund(self, original, method='CASH'):
        data = {
            'branch': self.branch.id,
            'original_invoice': original.id,
            'customer': self.customer.id,
            'refund_method': method,
            'items': [{'variant': self.variant.id, 'quantity': Decimal('2'),
                       'refund_amount': Decimal('200'), 'restock_inventory': True}],
        }
        ser = RefundInvoiceSerializer(data=data)
        ser.is_valid(raise_exception=True)
        return ser.save(store=self.store, created_by=self.owner)

    def test_restocking_fee_and_net(self):
        self.settings.restocking_fee_percent = Decimal('10')
        self.settings.save()
        refund = self._refund(self._make_invoice())
        self.assertEqual(refund.total_refunded, Decimal('200.00'))
        self.assertEqual(refund.restocking_fee, Decimal('20.00'))
        self.assertEqual(refund.net_refund, Decimal('180.00'))

    def test_no_fee_when_percent_zero(self):
        refund = self._refund(self._make_invoice())
        self.assertEqual(refund.restocking_fee, Decimal('0.00'))
        self.assertEqual(refund.net_refund, Decimal('200.00'))

    def test_store_credit_accrues_net(self):
        self.settings.restocking_fee_percent = Decimal('10')
        self.settings.save()
        self._refund(self._make_invoice(), method='STORE_CREDIT')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.store_credit, Decimal('180.00'))

    def test_cash_refund_does_not_touch_wallet(self):
        self._refund(self._make_invoice(), method='CASH')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.store_credit, Decimal('0.00'))

    def test_return_window_blocks_old_invoice(self):
        self.settings.return_window_days = 7
        self.settings.save()
        old = self._make_invoice(when=timezone.now() - timezone.timedelta(days=30))
        with self.assertRaises(ValidationError):
            self._refund(old)

    def test_return_window_allows_recent_invoice(self):
        self.settings.return_window_days = 7
        self.settings.save()
        recent = self._make_invoice(when=timezone.now() - timezone.timedelta(days=2))
        refund = self._refund(recent)
        self.assertIsNotNone(refund.refund_number)

    def test_zero_window_means_unlimited(self):
        old = self._make_invoice(when=timezone.now() - timezone.timedelta(days=999))
        refund = self._refund(old)  # must not raise
        self.assertIsNotNone(refund.refund_number)
