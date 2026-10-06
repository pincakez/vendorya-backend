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


class TillCheckoutPaymentTests(TestCase):
    """§AUDIT A2/A3 (s156): the till completes the sale AND takes the payment in ONE step.
    Before: checkout ran with paid=0, then a 2nd call always paid the FULL total — even with Ajel —
    so credit sales never became debt, and turning credit selling off blocked every till sale."""

    URL = '/api/finance/invoices/{}/checkout/'

    def setUp(self):
        from django.conf import settings as dj_settings
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken
        from finance.models import PaymentMethod
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_t', password='x', role='OWNER')
        self.store = Store.objects.create(name='T1', store_code='101', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.cashier = User.objects.create_user(username='csh_t', password='x', role='CASHIER', store=self.store)
        self.settings, _ = StoreSettings.objects.get_or_create(store=self.store)
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        sup = Supplier.objects.create(store=self.store, name='Sup', code_prefix='401', prefix_locked=True)
        product = Product.objects.create(store=self.store, name='Mouse', supplier=sup)
        self.variant = ProductVariant.objects.create(product=product, sell_price=Decimal('100'))
        StockLevel.objects.create(variant=self.variant, branch=self.branch, quantity=Decimal('10'))
        self.customer = Customer.objects.create(store=self.store, name='Buyer', phone_number='0101')
        self.walkin = Customer.objects.create(store=self.store, name='Walk-in', phone_number='0', is_walk_in=True)
        self.cash = PaymentMethod.objects.create(store=self.store, name='Cash', is_cash=True)
        self.agel = PaymentMethod.objects.create(store=self.store, name='Ajel', is_agel=True)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.cashier).access_token))

    def _draft(self, customer=None):
        inv = SalesInvoice.objects.create(store=self.store, branch=self.branch, customer=customer or self.customer,
                                          status=SalesInvoice.Status.DRAFT, date=timezone.now())
        SalesInvoiceItem.objects.create(invoice=inv, variant=self.variant, quantity=Decimal('1'),
                                        unit_price=Decimal('100'))
        # totals are normally computed by the serializer when the till saves the cart
        SalesInvoice.objects.filter(pk=inv.pk).update(grand_total=Decimal('100'))
        inv.refresh_from_db()
        return inv

    def _checkout(self, inv, method):
        return self.client.post(self.URL.format(inv.id), {'method': str(method.id)}, format='json')

    def test_ajel_sale_becomes_customer_debt(self):
        from finance.models import customer_outstanding, Payment
        inv = self._draft()
        r = self._checkout(inv, self.agel)
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SalesInvoice.Status.POSTED)
        self.assertEqual(inv.paid_amount, Decimal('0'))
        self.assertEqual(Payment.objects.filter(invoice=inv).count(), 0)
        self.assertEqual(customer_outstanding(self.customer), inv.grand_total)

    def test_cash_sale_paid_in_one_step(self):
        from finance.models import Payment
        inv = self._draft()
        r = self._checkout(inv, self.cash)
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, inv.grand_total)
        p = Payment.objects.get(invoice=inv)
        self.assertEqual((p.method, p.created_by), (self.cash, self.cashier))

    def test_cash_sale_works_when_credit_selling_is_off(self):
        self.settings.enable_agel_selling = False
        self.settings.save()
        self.assertEqual(self._checkout(self._draft(), self.cash).status_code, 200)

    def test_ajel_refused_when_credit_selling_is_off(self):
        self.settings.enable_agel_selling = False
        self.settings.save()
        inv = self._draft()
        self.assertEqual(self._checkout(inv, self.agel).status_code, 400)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SalesInvoice.Status.DRAFT)

    def test_cash_sale_not_blocked_by_credit_limit(self):
        self.settings.credit_policy = 'BLOCK'
        self.settings.save()
        self.customer.credit_limit = Decimal('0')
        self.customer.save()
        self.assertEqual(self._checkout(self._draft(), self.cash).status_code, 200)
        self.assertEqual(self._checkout(self._draft(), self.agel).status_code, 400)

    def test_ajel_needs_a_real_customer(self):
        self.assertEqual(self._checkout(self._draft(customer=self.walkin), self.agel).status_code, 400)

    def test_payments_endpoint_refuses_ajel_and_overpay(self):
        inv = self._draft()
        self._checkout(inv, self.agel)          # posted, unpaid 100
        url = '/api/finance/payments/'
        self.assertEqual(self.client.post(url, {'invoice': str(inv.id), 'method': str(self.agel.id), 'amount': '50'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {'invoice': str(inv.id), 'method': str(self.cash.id), 'amount': '500'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {'invoice': str(inv.id), 'method': str(self.cash.id), 'amount': '-5'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {'invoice': str(inv.id), 'method': str(self.cash.id), 'amount': '40'}, format='json').status_code, 201)
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('40'))
