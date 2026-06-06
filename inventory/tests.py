from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Store, StoreSettings, Branch, Address
from users.models import User
from inventory.models import (
    Supplier, Product, ProductVariant, StockLevel, StockAdjustment,
    Category, MAX_CATEGORY_DEPTH,
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


class CategoryTreeTests(TestCase):
    """Category tree is capped at MAX_CATEGORY_DEPTH tiers and rejects cycles."""

    def setUp(self):
        owner = User.objects.create_user(username='catowner', password='x')
        self.store = Store.objects.create(name='S1', store_code='200', owner=owner)

    def _cat(self, name, parent=None):
        return Category.objects.create(store=self.store, name=name, parent=parent)

    def test_depth_cap_blocks_too_deep(self):
        # Build a chain exactly MAX_CATEGORY_DEPTH deep — all should succeed.
        node = None
        chain = []
        for i in range(MAX_CATEGORY_DEPTH):
            node = self._cat(f'tier{i+1}', parent=node)
            chain.append(node)
        # One more tier under the deepest must be rejected.
        with self.assertRaises(ValidationError):
            self._cat('too_deep', parent=chain[-1])

    def test_reparent_pushing_subtree_too_deep_blocked(self):
        a = self._cat('A')                  # tier 1
        b = self._cat('B', parent=a)        # tier 2
        c = self._cat('C', parent=b)        # tier 3  (subtree height of A is 2)
        # Make a tier-2 node, then try to put A (height 2) under it -> 2+1+2=5 > 4.
        x = self._cat('X')                  # tier 1
        y = self._cat('Y', parent=x)        # tier 2
        a.parent = y
        with self.assertRaises(ValidationError):
            a.save()

    def test_cycle_self_parent_blocked(self):
        a = self._cat('A')
        a.parent = a
        with self.assertRaises(ValidationError):
            a.save()

    def test_cycle_descendant_parent_blocked(self):
        a = self._cat('A')
        b = self._cat('B', parent=a)
        a.parent = b           # b is a's child -> would be a cycle
        with self.assertRaises(ValidationError):
            a.save()

    def test_normal_tree_ok(self):
        a = self._cat('A')
        b = self._cat('B', parent=a)
        c = self._cat('C', parent=b)
        self.assertEqual(c.parent_id, b.id)
        self.assertEqual(b.parent_id, a.id)


class CategoryApiTests(TestCase):
    """The depth guard must surface as a clean 400 over the API, not a 500."""

    def setUp(self):
        from rest_framework.test import APIClient
        self.owner = User.objects.create_user(username='apiowner', password='x')
        self.store = Store.objects.create(name='S1', store_code='201', owner=self.owner)
        self.owner.store = self.store
        self.owner.role = User.Role.OWNER
        self.owner.save()
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def _post(self, name, parent=None):
        return self.client.post('/api/inventory/categories/',
                                {'name': name, 'parent': parent}, format='json')

    def test_too_deep_returns_400(self):
        ids = []
        parent = None
        for i in range(MAX_CATEGORY_DEPTH):
            r = self._post(f't{i}', parent)
            self.assertEqual(r.status_code, 201, r.content)
            parent = r.json()['id']
            ids.append(parent)
        # 5th tier -> blocked with a 400, not a server error.
        r = self._post('too_deep', parent)
        self.assertEqual(r.status_code, 400, r.content)

    def test_delete_with_children_blocked(self):
        a = self._post('A').json()['id']
        self._post('B', a)
        r = self.client.delete(f'/api/inventory/categories/{a}/')
        self.assertEqual(r.status_code, 400, r.content)
