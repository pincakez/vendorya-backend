"""s175 money pass: a shop that sells below zero, selling a product at a branch that never held it, really takes the stock
off (the sale used to move nothing — no stock row there); a service product still makes no stock row."""
from decimal import Decimal

from django.utils import timezone

from core.models import Address, Branch, StoreSettings
from finance.models import SalesInvoice, SalesInvoiceItem
from finance.tests import _TillBase
from inventory.models import Product, ProductVariant, StockLevel


class SellBelowZeroAtANewBranchTests(_TillBase):
    def setUp(self):
        super().setUp()
        StoreSettings.objects.filter(store=self.store).update(allow_negative_stock=True)
        self.b2 = Branch.objects.create(store=self.store, name='Second',
                                        address=Address.objects.create(store=self.store, street_1='2', city='Cairo'))
        self.cashier.default_branch = self.b2
        self.cashier.save(update_fields=['default_branch'])
        labour = Product.objects.create(store=self.store, name='Labour', product_type=Product.ProductType.SERVICE,
                                        supplier=self.variant.product.supplier)
        self.service = ProductVariant.objects.create(product=labour, sell_price=Decimal('30'))

    def test_the_sale_takes_the_stock_off_and_a_service_makes_no_row(self):
        inv = SalesInvoice.objects.create(store=self.store, branch=self.b2, customer=self.customer,
                                          status=SalesInvoice.Status.DRAFT, date=timezone.now())
        SalesInvoiceItem.objects.create(invoice=inv, variant=self.variant, quantity=Decimal('2'), unit_price=Decimal('100'))
        SalesInvoiceItem.objects.create(invoice=inv, variant=self.service, quantity=Decimal('1'), unit_price=Decimal('30'))
        SalesInvoice.objects.filter(pk=inv.pk).update(grand_total=Decimal('230'))
        inv.refresh_from_db()
        r = self._checkout(inv, self.cash)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(StockLevel.objects.get(variant=self.variant, branch=self.b2).quantity, Decimal('-2'))
        self.assertFalse(StockLevel.objects.filter(variant=self.service).exists())
        self.assertEqual(StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity, Decimal('10'))


class PurchaseStatusOnlyByReceiveTests(_TillBase):
    """s175 money pass: an edit can't move a purchase's status (RECEIVED → DRAFT → Receive again brought the stock in
    twice); Receive is the only door."""

    def test_edit_cannot_reopen_a_received_purchase(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken
        oc = APIClient()
        oc.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.owner).access_token))
        sup = self.variant.product.supplier
        r = oc.post('/api/finance/purchases/', {'supplier': str(sup.id), 'branch': str(self.branch.id),
                                                'date': timezone.now().isoformat(),
                                                'lines': [{'variant': str(self.variant.id), 'quantity': '5',
                                                           'base_price': '10'}]}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        pid = r.data['id']
        self.assertEqual(oc.post(f'/api/finance/purchases/{pid}/receive/').status_code, 200)
        self.assertEqual(StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity, Decimal('15'))
        r = oc.patch(f'/api/finance/purchases/{pid}/', {'status': 'DRAFT', 'paid_amount': '999'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.data['status'], r.data['paid_amount']), ('RECEIVED', '0.00'))
        self.assertEqual(oc.post(f'/api/finance/purchases/{pid}/receive/').status_code, 400)
        self.assertEqual(StockLevel.objects.get(variant=self.variant, branch=self.branch).quantity, Decimal('15'))
