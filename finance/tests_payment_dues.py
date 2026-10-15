"""s173 payment due dates: a reminder, never money — no balance / receipt / box moves; instalments add up exactly and
land on the right days; paid = a real customer receipt of the same customer; the day's reminder is sent once, only to
the writer + the owner; switch off refuses; roles; other shop."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, StoreSettings
from finance.models import CustomerReceipt, PaymentDue, customer_outstanding
from finance.payment_dues import add_months, notify_due, schedule, split
from finance.tests import _TillBase
from notifications.models import Notification
from users.models import Customer, User

URL = '/api/finance/payment-dues/'
D = Decimal


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class PaymentDueTests(_TillBase):
    def setUp(self):
        super().setUp()
        self.settings.payment_dues_enabled = True
        self.settings.save(update_fields=['payment_dues_enabled'])
        self.manager = User.objects.create_user(username='mgr_pd', password='x', role='MANAGER', store=self.store)
        self.mgr = _client(self.manager)
        self.customer.balance = D('1000')          # owes 1000 (opening balance)
        self.customer.save(update_fields=['balance'])

    def _new(self, **extra):
        body = {'customer': str(self.customer.id), 'amount': '1000',
                'first_date': (timezone.localdate() + timedelta(days=3)).isoformat(), **extra}
        r = self.mgr.post(URL, body, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return r.data if len(r.data) > 1 else r.data[0]     # instalments → the list; one date → that row

    def test_a_due_date_moves_no_money(self):
        self._new()
        self._new(count=4, every=1, unit='MONTH')
        self.assertEqual(customer_outstanding(self.customer), D('1000'))
        self.assertEqual(CustomerReceipt.objects.count(), 0)
        self.assertEqual(PaymentDue.objects.filter(store=self.store).count(), 5)

    def test_instalments_add_up_and_fall_on_the_right_days(self):
        rows = self._new(amount='1000', count=3, unit='MONTH', first_date='2026-01-31')
        self.assertEqual([r['amount'] for r in rows], ['333.33', '333.33', '333.34'])
        self.assertEqual([r['due_date'] for r in rows], ['2026-01-31', '2026-02-28', '2026-03-31'])
        self.assertEqual({r['plan'] for r in rows} != {None}, True)
        self.assertEqual([(r['plan_seq'], r['plan_count']) for r in rows], [(1, 3), (2, 3), (3, 3)])
        # helpers
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(schedule(date(2026, 1, 1), 3, 2, 'WEEK'), [date(2026, 1, 1), date(2026, 1, 15), date(2026, 1, 29)])
        self.assertEqual(sum(split(D('100'), 7)), D('100'))

    def test_paid_links_the_customers_receipt_and_needs_a_reason_without_one(self):
        due = self._new()
        # no receipt, no reason → refused
        r = self.mgr.post(f"{URL}{due['id']}/done/", {}, format='json')
        self.assertEqual(r.status_code, 400)
        rec = CustomerReceipt.objects.create(store=self.store, customer=self.customer, amount=D('1000'),
                                             method=self.cash, created_by=self.manager)
        other = Customer.objects.create(store=self.store, name='Other', phone_number='0199')
        rec_other = CustomerReceipt.objects.create(store=self.store, customer=other, amount=D('5'), method=self.cash)
        self.assertEqual(self.mgr.post(f"{URL}{due['id']}/done/", {'receipt': str(rec_other.id)},
                                       format='json').status_code, 400)
        r = self.mgr.post(f"{URL}{due['id']}/done/", {'receipt': str(rec.id)}, format='json')
        self.assertEqual((r.status_code, r.data['status'], str(r.data['receipt'])), (200, 'DONE', str(rec.id)), r.content)
        # closed → no second close / edit / cancel
        self.assertEqual(self.mgr.post(f"{URL}{due['id']}/cancel/", {'reason': 'x'}, format='json').status_code, 400)
        self.assertEqual(self.mgr.patch(f"{URL}{due['id']}/", {'note': 'x'}, format='json').status_code, 400)

    def test_move_the_date_resets_the_reminder_and_filters(self):
        today = timezone.localdate()
        late = self._new(first_date=(today - timedelta(days=2)).isoformat())
        self._new(first_date=today.isoformat())
        self._new(first_date=(today + timedelta(days=5)).isoformat())
        self._new(first_date=(today + timedelta(days=30)).isoformat())
        s = self.mgr.get(f'{URL}summary/').data
        self.assertEqual((s['late']['count'], s['today']['count'], s['soon']['count']), (1, 1, 1))
        got = self.mgr.get(URL, {'due': 'late'}).json()
        self.assertEqual([r['id'] for r in (got['results'] if isinstance(got, dict) else got)], [late['id']])
        PaymentDue.objects.filter(pk=late['id']).update(notified_on=today)
        r = self.mgr.patch(f"{URL}{late['id']}/", {'due_date': (today + timedelta(days=10)).isoformat()}, format='json')
        self.assertEqual((r.status_code, PaymentDue.objects.get(pk=late['id']).notified_on), (200, None))

    def test_the_days_reminder_is_sent_once_to_the_writer_and_the_owner_only(self):
        today = timezone.localdate()
        self._new(first_date=today.isoformat())
        self._new(first_date=(today + timedelta(days=4)).isoformat())    # not yet
        self.assertEqual(notify_due(self.store), 1)
        notes = Notification.objects.filter(store=self.store, type=Notification.Type.PAYMENT_DUE)
        self.assertEqual(sorted(n.user_id for n in notes), sorted([self.manager.id, self.owner.id]))
        self.assertTrue(all(n.payload['i18n']['title'] == 'payment_due.title' for n in notes))
        self.assertEqual(notify_due(self.store), 0)                       # once
        # the bell triggers it, and a cashier's bell never shows it
        self._new(first_date=(today - timedelta(days=1)).isoformat())
        _client(self.cashier).get('/api/notifications/unread-count/')
        self.assertEqual(Notification.objects.filter(type=Notification.Type.PAYMENT_DUE, user=self.cashier).count(), 0)
        self.assertEqual(Notification.objects.filter(type=Notification.Type.PAYMENT_DUE).count(), 4)

    def test_many_at_once_send_one_summary(self):
        today = timezone.localdate()
        self._new(count=8, every=1, unit='DAY', first_date=(today - timedelta(days=10)).isoformat())
        self.assertEqual(notify_due(self.store), 8)
        mine = Notification.objects.filter(type=Notification.Type.PAYMENT_DUE, user=self.manager)
        self.assertEqual((mine.count(), mine.first().payload['i18n']['title']), (1, 'payment_due.many_title'))

    def test_switch_off_roles_and_bad_input(self):
        self.assertEqual(_client(self.cashier).get(URL).status_code, 403)
        for bad in ({'amount': '0'}, {'count': 0}, {'count': 61}, {'unit': 'YEAR'}, {'first_date': ''},
                    {'customer': str(self.walkin.id)}, {'amount': '0.05', 'count': 6}):
            body = {'customer': str(self.customer.id), 'amount': '100',
                    'first_date': timezone.localdate().isoformat(), **bad}
            self.assertEqual(self.mgr.post(URL, body, format='json').status_code, 400, bad)
        self.settings.payment_dues_enabled = False
        self.settings.save(update_fields=['payment_dues_enabled'])
        r = self.mgr.post(URL, {'customer': str(self.customer.id), 'amount': '100',
                                'first_date': timezone.localdate().isoformat()}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(notify_due(self.store), 0)

    def test_other_shop_is_invisible(self):
        due = self._new()
        own2 = User.objects.create_user(username='own_pd2', password='x', role='OWNER')
        shop2 = Store.objects.create(name='T2', store_code='102', owner=own2)
        own2.store = shop2
        own2.save(update_fields=['store'])
        StoreSettings.objects.filter(store=shop2).update(payment_dues_enabled=True)
        c2 = _client(own2)
        self.assertEqual(c2.get(f"{URL}{due['id']}/").status_code, 404)
        self.assertEqual(c2.post(f"{URL}{due['id']}/cancel/", {'reason': 'x'}, format='json').status_code, 404)
        r = c2.post(URL, {'customer': str(self.customer.id), 'amount': '5',
                          'first_date': timezone.localdate().isoformat()}, format='json')
        self.assertEqual(r.status_code, 400)
