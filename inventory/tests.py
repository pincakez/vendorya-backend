from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Store, StoreSettings, Branch, Address
from users.models import User
from inventory.models import (
    Supplier, Product, ProductVariant, StockLevel, StockAdjustment,
)


class StockAdjustmentNegativeStockTests(TestCase):
    """A manual stock adjustment must obey the store's allow_negative_stock
    policy exactly like a POS sale does — and roll back cleanly when it can't."""

    def setUp(self):
        self.owner = User.objects.create_user(username='owner1', password='x')
        self.store = Store.objects.create(name='S1', store_code='100', owner=self.owner)
        # A post_save signal auto-creates StoreSettings; just grab and configure it.
        self.settings, _ = StoreSettings.objects.get_or_create(store=self.store)
        self.settings.allow_negative_stock = False
        self.settings.save()
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        self.supplier = Supplier.objects.create(
            store=self.store, name='Sup', code_prefix='400', prefix_locked=True)
        product = Product.objects.create(store=self.store, name='Laptop', supplier=self.supplier)
        self.variant = ProductVariant.objects.create(product=product)
        StockLevel.objects.create(variant=self.variant, branch=self.branch, quantity=Decimal('5'))

    def _adjust(self, change):
        return StockAdjustment.objects.create(
            store=self.store, branch=self.branch, variant=self.variant,
            quantity_change=Decimal(change), reason=StockAdjustment.Reason.THEFT,
            adjusted_by=self.owner,
        )

    def _stock(self):
        return StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity

    def test_block_when_negative_not_allowed(self):
        with self.assertRaises(ValidationError):
            self._adjust('-7')
        # Nothing persisted: stock unchanged AND the ledger row rolled back.
        self.assertEqual(self._stock(), Decimal('5'))
        self.assertEqual(StockAdjustment.all_objects.filter(store=self.store).count(), 0)

    def test_allow_when_policy_permits(self):
        self.settings.allow_negative_stock = True
        self.settings.save()
        self._adjust('-7')
        self.assertEqual(self._stock(), Decimal('-2'))

    def test_normal_reduction_within_stock_ok(self):
        self._adjust('-3')
        self.assertEqual(self._stock(), Decimal('2'))

    def test_positive_adjustment_always_ok(self):
        self._adjust('10')
        self.assertEqual(self._stock(), Decimal('15'))
