from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, StoreSettings, Branch, Address
from users.models import User, Customer
from inventory.models import Supplier, Product, ProductVariant, StockLevel
from finance.models import SalesInvoice, SalesInvoiceItem
from finance.serializers import RefundInvoiceSerializer


class _ReportBase(TestCase):
    """One shop + owner + a 100-priced laptop, for the report tests."""

    def setUp(self):
        from django.conf import settings as dj_settings
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_pl', password='x', role='OWNER')
        self.store = Store.objects.create(name='PL', store_code='120', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.settings, _ = StoreSettings.objects.get_or_create(store=self.store)
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        sup = Supplier.objects.create(store=self.store, name='Sup', code_prefix='420', prefix_locked=True)
        product = Product.objects.create(store=self.store, name='Laptop', supplier=sup)
        self.variant = ProductVariant.objects.create(product=product, sell_price=Decimal('100'))
        StockLevel.objects.create(variant=self.variant, branch=self.branch, quantity=Decimal('10'))
        self.customer = Customer.objects.create(store=self.store, name='Buyer', phone_number='0120')
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.owner).access_token))

    def _sale(self):
        """1 × 100 + 14 % tax = 114, cost 60 (written directly: the report reads stored figures)."""
        inv = SalesInvoice.objects.create(store=self.store, branch=self.branch, customer=self.customer,
                                          status=SalesInvoice.Status.POSTED, date=timezone.now())
        item = SalesInvoiceItem.objects.create(invoice=inv, variant=self.variant, quantity=Decimal('1'),
                                               unit_price=Decimal('100'), tax_amount=Decimal('14'))
        SalesInvoiceItem.objects.filter(pk=item.pk).update(cost_at_sale=Decimal('60'))
        SalesInvoice.objects.filter(pk=inv.pk).update(subtotal=Decimal('100'), tax_total=Decimal('14'),
                                                      grand_total=Decimal('114'))
        inv.refresh_from_db()
        return inv

    def _refund(self, inv, restock=True):
        ser = RefundInvoiceSerializer(data={
            'branch': self.branch.id, 'original_invoice': inv.id, 'customer': self.customer.id,
            'items': [{'variant': self.variant.id, 'quantity': Decimal('1'), 'refund_amount': Decimal('114'),
                       'restock_inventory': restock}],
        })
        ser.is_valid(raise_exception=True)
        return ser.save(store=self.store, created_by=self.owner)

    def _pnl(self):
        r = self.client.get('/api/reports/pnl/')
        self.assertEqual(r.status_code, 200, r.content)
        return {k: (Decimal(v) if k != 'reconciles' else v) for k, v in r.data['totals'].items()}


class ProfitLossReturnsTests(_ReportBase):
    """§WALK-S163 B2 (s165): returns in the P&L follow standard accounting — counted without tax (revenue
    is ex-tax), the restocking fee the shop keeps is income, and the cost of restocked goods comes back
    out of COGS. Before: returns were taken WITH tax, before the fee, and their cost never came back."""

    def test_sale_alone(self):
        self._sale()
        t = self._pnl()
        self.assertEqual((t['revenue'], t['cogs'], t['returns'], t['net']),
                         (Decimal('100'), Decimal('60'), Decimal('0'), Decimal('40')))

    def test_full_return_restocked_with_fee_leaves_only_the_fee(self):
        self.settings.restocking_fee_percent = Decimal('10')
        self.settings.save()
        self._refund(self._sale())                       # refund 114, fee 11.40
        t = self._pnl()
        self.assertEqual(t['revenue'], Decimal('100'))
        self.assertEqual(t['returns_ex_tax'], Decimal('100'))    # was 114 (tax included)
        self.assertEqual(t['restock_fees'], Decimal('11.40'))
        self.assertEqual(t['returns'], Decimal('88.60'))
        self.assertEqual(t['cogs_returned'], Decimal('60'))
        self.assertEqual(t['cogs'], Decimal('0'))                # the laptop is back on the shelf
        self.assertEqual(t['gross_profit'], Decimal('11.40'))
        self.assertEqual(t['net'], Decimal('11.40'))             # the shop earned exactly the fee
        self.assertTrue(t['reconciles'])

    def test_return_not_restocked_keeps_the_cost(self):
        self._refund(self._sale(), restock=False)        # damaged — thrown away
        t = self._pnl()
        self.assertEqual(t['cogs'], Decimal('60'))
        self.assertEqual(t['returns'], Decimal('100'))
        self.assertEqual(t['net'], Decimal('-60'))               # the shop lost the item's cost


class TaxReportSnapshotTests(_ReportBase):
    """§AUDIT A12 (s165): the tax report groups by the rate frozen on each sale line, so changing a
    product's tax later no longer rewrites old reports."""

    def test_changing_a_products_tax_keeps_old_reports(self):
        from inventory.models import Tax
        from finance.serializers import SalesInvoiceSerializer
        vat = Tax.objects.create(store=self.store, name='VAT 14', rate=Decimal('14'))
        self.variant.product.tax = vat
        self.variant.product.save()
        inv = SalesInvoice.objects.create(store=self.store, branch=self.branch, customer=self.customer,
                                          status=SalesInvoice.Status.POSTED, date=timezone.now())
        SalesInvoiceItem.objects.create(invoice=inv, variant=self.variant, quantity=Decimal('2'),
                                        unit_price=Decimal('100'), discount_amount=Decimal('20'))
        SalesInvoiceSerializer._recalculate(inv)          # taxable 180 → tax 25.20
        low = Tax.objects.create(store=self.store, name='VAT 5', rate=Decimal('5'))
        self.variant.product.tax = low                    # the owner changes the product's tax AFTER the sale
        self.variant.product.save()
        r = self.client.get('/api/reports/tax/')
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.data['by_rate']
        self.assertEqual(len(rows), 1)
        self.assertEqual(Decimal(rows[0]['rate']), Decimal('14'))
        self.assertEqual(rows[0]['name'], 'VAT 14')
        self.assertEqual(Decimal(rows[0]['taxable']), Decimal('180'))   # was 200 (discount ignored)
        self.assertEqual(Decimal(rows[0]['collected']), Decimal('25.20'))
