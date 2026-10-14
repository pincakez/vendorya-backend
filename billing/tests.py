from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.models import Store, Branch, Address
from users.models import User, Customer
from finance.models import SalesInvoice
from billing.models import BillingSettings, Subscription, SubscriptionPlan
from billing.quota import enforce_quota, quota_status


class InvoiceQuotaCountsPostedOnlyTests(TestCase):
    """The monthly invoice quota counts COMPLETED sales only (s165). Before, every DRAFT cart the
    till opened counted too, so a shop could hit its limit without selling anything."""

    def setUp(self):
        owner = User.objects.create_user(username='q_own', password='x', role='OWNER')
        self.store = Store.objects.create(name='Q1', store_code='201', owner=owner)
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        self.customer = Customer.objects.create(store=self.store, name='C', phone_number='0102')
        plan = SubscriptionPlan.objects.create(name='Quota Test Plan', max_invoices_per_month=2)
        Subscription.all_objects.update_or_create(store=self.store, defaults={'plan': plan})
        bs = BillingSettings.load()
        bs.quota_mode = BillingSettings.QuotaMode.BLOCK
        bs.save()

    def _invoice(self, status, deleted=False):
        return SalesInvoice.all_objects.create(
            store=self.store, branch=self.branch, customer=self.customer,
            status=status, date=timezone.now(), grand_total=Decimal('10'), is_deleted=deleted,
        )

    def test_drafts_do_not_use_the_quota(self):
        for _ in range(5):
            self._invoice(SalesInvoice.Status.DRAFT)
        self.assertEqual(quota_status(self.store)['invoices']['used'], 0)
        self.assertEqual(enforce_quota(self.store, 'invoices'), 2)   # still allowed

    def test_posted_invoices_count_and_block_at_the_limit(self):
        self._invoice(SalesInvoice.Status.DRAFT)
        self._invoice(SalesInvoice.Status.POSTED)
        self._invoice(SalesInvoice.Status.POSTED, deleted=True)   # deleted → not counted
        self.assertEqual(quota_status(self.store)['invoices']['used'], 1)
        enforce_quota(self.store, 'invoices')                      # 1 < 2 → allowed
        self._invoice(SalesInvoice.Status.POSTED)
        status = quota_status(self.store)['invoices']
        self.assertEqual((status['used'], status['over']), (2, True))
        with self.assertRaises(ValidationError):
            enforce_quota(self.store, 'invoices')
