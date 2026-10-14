"""PH5 — purchase in packs (s127 unit engine).

A purchase line may be received in an alternate selling unit (e.g. a Pack of 10).
The real API path is: POST /api/finance/purchases/ with `lines[].unit` →
PurchaseInvoiceSerializer._resolve_unit freezes PurchaseItem.unit + unit_factor →
POST .../receive/ → handle_purchase_stock converts qty × factor into BASE units and
divides the pack cost by the factor for the per-piece cost.

Run: DB_TEST_NAME=test_vdev_ph5 venv/bin/python manage.py test finance.tests_purchase_units --noinput
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from core.models import Store, StoreSettings, Branch, Address
from users.models import User
from inventory.models import (
    Supplier, Product, ProductVariant, ProductUnit, StockLevel, StockBatch,
)
from finance.models import PurchaseInvoice, PurchaseItem


class PurchaseInPacksTests(TestCase):
    LIST_URL = '/api/finance/purchases/'
    RECEIVE_URL = '/api/finance/purchases/{}/receive/'

    def setUp(self):
        from django.conf import settings as dj_settings
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken
        dj_settings.ALLOWED_HOSTS = ['*']

        self.owner = User.objects.create_user(username='own_ph5', password='x', role='OWNER')
        self.store = Store.objects.create(name='PH5', store_code='105', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.manager = User.objects.create_user(
            username='mgr_ph5', password='x', role='MANAGER', store=self.store)

        self.settings, _ = StoreSettings.objects.get_or_create(store=self.store)
        self.settings.multi_unit_enabled = True          # unit selling allowed
        self.settings.save(update_fields=['multi_unit_enabled'])

        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        self.supplier = Supplier.objects.create(
            store=self.store, name='Pharma Sup', code_prefix='405', prefix_locked=True)

        self.product = Product.objects.create(
            store=self.store, name='Paracetamol 500', supplier=self.supplier)
        self.variant = ProductVariant.objects.create(
            product=self.product, sell_price=Decimal('3'), cost_price=Decimal('1'))
        # 1 Pack = 10 pieces (base unit)
        self.pack = ProductUnit.objects.create(
            variant=self.variant, name='Pack', factor=Decimal('10'), sell_price=Decimal('30'))
        StockLevel.objects.create(variant=self.variant, branch=self.branch, quantity=Decimal('7'))

        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.manager).access_token))

    # --- helpers -------------------------------------------------------------
    def _create_purchase(self, line_extra=None):
        line = {
            'variant': str(self.variant.id),
            'unit': str(self.pack.id),
            'quantity': '50',          # 50 packs
            'base_price': '25.00',     # cost per PACK
        }
        line.update(line_extra or {})
        res = self.client.post(self.LIST_URL, {
            'supplier': str(self.supplier.id),
            'branch': str(self.branch.id),
            'date': timezone.now().isoformat(),
            'lines': [line],
        }, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        return PurchaseInvoice.objects.get(pk=res.data['id'])

    def _receive(self, invoice):
        res = self.client.post(self.RECEIVE_URL.format(invoice.id), {}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        invoice.refresh_from_db()
        return invoice

    # --- tests ---------------------------------------------------------------
    def test_draft_line_freezes_pack_unit_and_factor(self):
        inv = self._create_purchase()
        self.assertEqual(inv.status, PurchaseInvoice.Status.DRAFT)
        item = PurchaseItem.objects.get(invoice=inv)
        self.assertEqual(item.unit_id, self.pack.id)
        self.assertEqual(item.unit_factor, Decimal('10'))
        # quantity + unit_cost stay in the purchased unit (packs)
        self.assertEqual(item.quantity, Decimal('50'))
        self.assertEqual(item.unit_cost, Decimal('25.00'))
        self.assertEqual(item.total_cost, Decimal('1250.00'))
        self.assertEqual(inv.total_amount, Decimal('1250.00'))
        # drafts move no stock
        self.assertEqual(StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity,
                         Decimal('7'))

    def test_receive_50_packs_adds_500_base_units_and_piece_cost(self):
        inv = self._create_purchase()
        inv = self._receive(inv)
        self.assertEqual(inv.status, PurchaseInvoice.Status.RECEIVED)

        stock = StockLevel.objects.get(variant=self.variant, branch=self.branch)
        self.assertEqual(stock.quantity, Decimal('507'))   # 7 + 50 × 10

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.cost_price, Decimal('2.50'))   # 25 ÷ 10 per piece

        item = PurchaseItem.objects.get(invoice=inv)
        self.assertEqual(item.unit_factor, Decimal('10'))

        # API round-trip: the read shape keeps the pack selection
        res = self.client.get(f'{self.LIST_URL}{inv.id}/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['items'][0]['unit'], str(self.pack.id))

    def test_receive_expiry_tracked_pack_creates_batch_in_base_units(self):
        """The only stock "ledger" row the receive writes is a StockBatch (expiry-tracked
        products) — it must be counted in BASE units at the per-piece cost."""
        self.settings.expiry_tracking_enabled = True
        self.settings.save(update_fields=['expiry_tracking_enabled'])
        self.product.track_expiry = True
        self.product.save(update_fields=['track_expiry'])

        inv = self._create_purchase({'expiry_date': '2028-06-30', 'batch_number': 'LOT-PH5'})
        self._receive(inv)

        item = PurchaseItem.objects.get(invoice=inv)
        batch = StockBatch.objects.get(variant=self.variant, branch=self.branch,
                                       source_purchase_item=item)
        self.assertEqual(batch.quantity_remaining, Decimal('500'))
        self.assertEqual(batch.cost_per_base, Decimal('2.50'))
        self.assertEqual(batch.expiry_date, date(2028, 6, 30))
        self.assertEqual(batch.batch_number, 'LOT-PH5')
        self.assertEqual(StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity,
                         Decimal('507'))

    def test_base_unit_line_is_factor_1(self):
        inv = self._create_purchase({'unit': None, 'quantity': '12', 'base_price': '2.40'})
        item = PurchaseItem.objects.get(invoice=inv)
        self.assertIsNone(item.unit_id)
        self.assertEqual(item.unit_factor, Decimal('1'))
        self._receive(inv)
        self.assertEqual(StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity,
                         Decimal('19'))
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.cost_price, Decimal('2.40'))

    def test_foreign_unit_is_rejected_falls_back_to_base(self):
        """A unit belonging to ANOTHER variant must never freeze its factor here."""
        other_p = Product.objects.create(store=self.store, name='Other', supplier=self.supplier)
        other_v = ProductVariant.objects.create(product=other_p, sell_price=Decimal('1'))
        foreign = ProductUnit.objects.create(variant=other_v, name='Case', factor=Decimal('100'))
        inv = self._create_purchase({'unit': str(foreign.id), 'quantity': '3'})
        item = PurchaseItem.objects.get(invoice=inv)
        self.assertIsNone(item.unit_id)
        self.assertEqual(item.unit_factor, Decimal('1'))
        self._receive(inv)
        self.assertEqual(StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity,
                         Decimal('10'))   # 7 + 3, NOT 7 + 300
