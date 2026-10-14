"""s167 (PROLINE-PARITY Upgrade 12): notifications carry a stable message key + params
(payload['i18n']) so each reader's screen shows them in its own language; the stored
English title/body stay as the fallback. Plus: the print-data carries the shop language."""
from decimal import Decimal

from django.test import TestCase

from inventory.models import StockLevel
from finance.tests import _TillBase
from notifications.dispatcher import msg, qty_str, send_notification, word
from notifications.models import Notification


class MessageKeyHelperTests(TestCase):

    def test_msg_shape_and_param_normalising(self):
        m = msg('low_stock.title', 'low_stock.body', product='Mouse', qty=qty_str(Decimal('3.000')),
                total=Decimal('150.00'), n=4, who=word('walk_in'), empty=None)
        self.assertEqual(m['title'], 'low_stock.title')
        self.assertEqual(m['body'], 'low_stock.body')
        self.assertEqual(m['params'], {'product': 'Mouse', 'qty': '3', 'total': '150.00', 'n': 4,
                                       'who': {'k': 'walk_in'}, 'empty': ''})

    def test_no_body_key_means_stored_body_is_shown(self):
        self.assertNotIn('body', msg('service_diagnosis.title', serial='S1'))

    def test_qty_str(self):
        self.assertEqual(qty_str(Decimal('2.500')), '2.5')
        self.assertEqual(qty_str(Decimal('100')), '100')


class NotificationKeyTests(_TillBase):

    def _mgr_client(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken
        from users.models import User
        mgr = User.objects.create_user(username='mgr_n', password='x', role='MANAGER', store=self.store)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(mgr).access_token))
        return c

    def test_send_notification_stores_key_and_keeps_existing_payload(self):
        n = send_notification(self.store, title='Hello', payload={'service_id': 'x'},
                              i18n=msg('service_cost.title', 'service_cost.body', serial='S9', cost='10.00'))
        n.refresh_from_db()
        self.assertEqual(n.title, 'Hello')                         # English fallback still stored
        self.assertEqual(n.payload['service_id'], 'x')
        self.assertEqual(n.payload['i18n']['title'], 'service_cost.title')
        self.assertEqual(n.payload['i18n']['params'], {'serial': 'S9', 'cost': '10.00'})

    def test_old_style_call_has_no_i18n_block(self):
        n = send_notification(self.store, title='Plain')
        self.assertNotIn('i18n', n.payload)

    def test_low_stock_on_checkout_stores_key_and_params(self):
        StockLevel.objects.filter(variant=self.variant, branch=self.branch).update(quantity=Decimal('4'))
        inv = self._draft()
        self.assertEqual(self._checkout(inv, self.cash).status_code, 200)
        n = Notification.all_objects.get(store=self.store, type=Notification.Type.LOW_STOCK)
        m = n.payload['i18n']
        self.assertEqual((m['title'], m['body']), ('low_stock.title', 'low_stock.body'))
        self.assertEqual(m['params']['product'], 'Mouse')
        self.assertEqual(m['params']['qty'], '3')                   # 4 − 1, no trailing zeros
        self.assertEqual(m['params']['branch'], 'Main')

    def test_void_and_shift_difference_store_keys(self):
        r = self.client.post('/api/finance/shifts/', {'branch': str(self.branch.id), 'starting_cash': '100'},
                             format='json')
        self.assertEqual(r.status_code, 201, r.content)
        inv = self._draft()
        self.assertEqual(self._checkout(inv, self.cash).status_code, 200)
        inv.refresh_from_db()
        self.assertEqual(self._mgr_client().post(f'/api/finance/invoices/{inv.id}/void/').status_code, 200)
        v = Notification.all_objects.get(store=self.store, type=Notification.Type.INVOICE_VOIDED)
        self.assertEqual(v.payload['i18n']['title'], 'invoice_voided.title')
        self.assertEqual(v.payload['i18n']['params']['number'], inv.invoice_number)

        r = self.client.post(f"/api/finance/shifts/{r.data['id']}/close/", {'counted_cash': '90'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        s = Notification.all_objects.get(store=self.store, type=Notification.Type.SHIFT_DIFFERENCE)
        self.assertEqual(s.payload['i18n']['title'], 'shift_short.title')
        self.assertEqual(s.payload['i18n']['body'], 'shift_diff.body')
        self.assertTrue(s.payload['i18n']['params']['diff'].startswith('-'))

    def test_api_exposes_payload_i18n_to_the_reader(self):
        send_notification(self.store, title='T', i18n=msg('expiry.title', 'expiry.body_soon', soon=2, days=30))
        r = self.client.get('/api/notifications/recent/')
        self.assertEqual(r.status_code, 200, r.content)
        row = next(x for x in r.data if x['title'] == 'T')
        self.assertEqual(row['payload']['i18n']['body'], 'expiry.body_soon')


class PrintDataLanguageTests(_TillBase):

    def test_print_data_carries_the_shop_language(self):
        for lang in ('ar', 'en'):
            self.store.default_language = lang
            self.store.save(update_fields=['default_language'])
            inv = self._draft()
            r = self.client.get(f'/api/finance/invoices/{inv.id}/print-data/')
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(r.data['store']['default_language'], lang)
