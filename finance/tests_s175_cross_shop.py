"""s175 security review: a line that names ANOTHER shop's product variant is refused everywhere a variant is typed by id
(sale cart, stock correction, transfer, return) — Store A can never sell, count or move Store B's stock. Each case runs the
same body with the shop's OWN variant first, so a refusal is the shop check and not a wrong body. Plus: a return to a
branch that never held the product no longer crashes (a new stock row starts as a float)."""
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Address, Branch, Store
from finance.tests import _TillBase
from inventory.models import Product, ProductVariant, StockLevel, Supplier
from users.models import User


class CrossShopVariantTests(_TillBase):
    def setUp(self):
        super().setUp()
        o = User.objects.create_user(username='own_b', password='x', role='OWNER')
        self.other = Store.objects.create(name='B', store_code='102', owner=o)
        o.store = self.other
        o.save(update_fields=['store'])
        other_branch = Branch.objects.create(store=self.other, name='B main',
                                             address=Address.objects.create(store=self.other, street_1='1', city='Giza'))
        sup = Supplier.objects.create(store=self.other, name='SupB', code_prefix='402', prefix_locked=True)
        p = Product.objects.create(store=self.other, name='Secret', supplier=sup)
        self.foreign = ProductVariant.objects.create(product=p, sell_price=Decimal('50'))
        StockLevel.objects.create(variant=self.foreign, branch=other_branch, quantity=Decimal('7'))
        self.oc = APIClient()
        self.oc.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.owner).access_token))
        self.b2 = Branch.objects.create(store=self.store, name='Second',
                                        address=Address.objects.create(store=self.store, street_1='2', city='Cairo'))

    def _own_ok_foreign_refused(self, client, url, body):
        ok = client.post(url, body(self.variant), format='json')
        self.assertIn(ok.status_code, (200, 201), ok.content)
        bad = client.post(url, body(self.foreign), format='json')
        self.assertEqual(bad.status_code, 400, bad.content)
        self.assertEqual(StockLevel.objects.get(variant=self.foreign).quantity, Decimal('7'))

    def test_sale_cart(self):
        self._own_ok_foreign_refused(self.client, '/api/finance/invoices/', lambda v: {
            'branch': str(self.branch.id), 'customer': str(self.customer.id), 'status': 'DRAFT',
            'date': timezone.now().isoformat(),
            'items': [{'variant': str(v.id), 'quantity': '1', 'unit_price': '50'}]})

    def test_stock_correction(self):
        self._own_ok_foreign_refused(self.oc, '/api/inventory/adjustments/', lambda v: {
            'branch': str(self.branch.id), 'variant': str(v.id), 'quantity_change': '1', 'reason': 'CORRECTION'})

    def test_transfer(self):
        self._own_ok_foreign_refused(self.oc, '/api/inventory/transfers/', lambda v: {
            'from_branch': str(self.branch.id), 'to_branch': str(self.b2.id),
            'items': [{'variant': str(v.id), 'quantity': '1'}]})

    def test_return_without_a_sale(self):
        # the shop's own product returned at a branch that never held it — used to crash (float + Decimal)
        self._own_ok_foreign_refused(self.oc, '/api/finance/refunds/', lambda v: {
            'branch': str(self.b2.id), 'customer': str(self.customer.id), 'reason': 'x', 'refund_method': 'CASH',
            'items': [{'variant': str(v.id), 'quantity': '1', 'refund_amount': '0', 'restock_inventory': True}]})
        self.assertEqual(StockLevel.objects.get(variant=self.variant, branch=self.b2).quantity, Decimal('1'))
