from decimal import Decimal

from core.models import ActivityLog
from finance.models import Payment
from finance.tests import _TillBase


class PaymentActivityLogTests(_TillBase):
    """Money taken through the Payments API (e.g. a customer paying off an Ajel sale) leaves a row in
    the Activity Log (s165). Before, only the till checkout was logged; a later payment left no trace."""

    URL_PAY = '/api/finance/payments/'

    def test_payment_writes_one_activity_row(self):
        inv = self._draft()
        self.assertEqual(self._checkout(inv, self.agel).status_code, 200)   # posted, owes 100
        inv.refresh_from_db()
        before = ActivityLog.all_objects.filter(store=self.store).count()
        r = self.client.post(self.URL_PAY, {'invoice': str(inv.id), 'method': str(self.cash.id),
                                            'amount': '40'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        logs = ActivityLog.all_objects.filter(store=self.store).order_by('-timestamp')
        self.assertEqual(logs.count(), before + 1)
        row = logs.first()
        pay = Payment.objects.get(pk=r.data['id'])
        self.assertEqual(row.user, self.cashier)
        self.assertEqual(row.operation_type, ActivityLog.OperationType.SALE)
        self.assertIn(str(inv.invoice_number), row.action)
        self.assertEqual(row.details['payment_id'], str(pay.id))
        self.assertEqual(row.details['invoice_id'], str(inv.id))
        self.assertEqual(Decimal(row.details['amount']), Decimal('40'))
        self.assertEqual(row.details['method'], 'Cash')
        self.assertEqual(row.details['customer'], 'Buyer')

    def test_refused_payment_writes_nothing(self):
        inv = self._draft()
        self._checkout(inv, self.agel)
        before = ActivityLog.all_objects.filter(store=self.store).count()
        r = self.client.post(self.URL_PAY, {'invoice': str(inv.id), 'method': str(self.cash.id),
                                            'amount': '500'}, format='json')   # more than owed
        self.assertEqual(r.status_code, 400)
        self.assertEqual(ActivityLog.all_objects.filter(store=self.store).count(), before)
