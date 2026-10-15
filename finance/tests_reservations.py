"""s172 reservations with a deposit (RESERVATIONS.md): held stock is not for sale elsewhere, a deposit is real money
counted ONCE (box line / shift) and never again when the reservation becomes a sale, convert runs the till's own rules,
cancel gives back / keeps (other income) / moves to store credit, roles, switch, other shop."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Address, Branch, Store, StoreSettings
from finance.models import Payment, PaymentMethod, Reservation, SalesInvoice, WorkShift, customer_outstanding
from finance.tests import _TillBase
from inventory.models import Product, ProductVariant, StockLevel, Supplier
from treasury import ledger
from treasury.models import MoneyBox, MoneyMove
from users.models import Customer, User

URL = '/api/finance/reservations/'
D = Decimal


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class ReservationTests(_TillBase):
    TREASURY = False

    def setUp(self):
        super().setUp()
        self.manager = User.objects.create_user(username='mgr_rs', password='x', role='MANAGER', store=self.store)
        self.mgr = _client(self.manager)
        self.card = PaymentMethod.objects.create(store=self.store, name='Card')
        StoreSettings.objects.filter(store=self.store).update(reservations_enabled=True,
                                                              treasury_enabled=self.TREASURY)
        if self.TREASURY:
            ledger.setup(self.store, user=self.owner)
            self.drawer = ledger.default_drawer(self.branch)
            self.bank = ledger.open_box(part=ledger.shop_part(self.store), name='Bank', kind=MoneyBox.Kind.BANK)
            PaymentMethod.objects.filter(pk=self.card.pk).update(lands_in=self.bank)
            self.card.refresh_from_db()

    def _new(self, qty='3', price='100', deposit=None, method=None, client=None, **extra):
        body = {'branch': str(self.branch.id), 'customer': str(self.customer.id),
                'items': [{'variant': str(self.variant.id), 'quantity': qty, 'unit_price': price}], **extra}
        if deposit is not None:
            body.update(deposit_amount=deposit, deposit_method=str((method or self.cash).id))
        return (client or self.client).post(URL, body, format='json')

    def _shift(self, user=None):
        r = _client(user or self.cashier).post('/api/finance/shifts/', {'branch': str(self.branch.id),
                                                                         'starting_cash': '0'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return WorkShift.objects.get(pk=r.data['id'])

    # ── holding ────────────────────────────────────────────────────────────────────────────────────────────────
    def test_switch_off_refuses_new_reservations(self):
        StoreSettings.objects.filter(store=self.store).update(reservations_enabled=False)
        r = self._new()
        self.assertEqual((r.status_code, r.data['code']), (400, 'reservations_off'), r.content)

    def test_walk_in_and_too_much_refused(self):
        r = self.client.post(URL, {'branch': str(self.branch.id), 'customer': str(self.walkin.id),
                                   'items': [{'variant': str(self.variant.id), 'quantity': '1', 'unit_price': '100'}]},
                             format='json')
        self.assertEqual(r.data['code'], 'customer_required')
        r = self._new(qty='11')
        self.assertEqual(r.data['code'], 'not_enough_stock', r.content)

    def test_held_units_are_not_for_sale_at_the_till(self):
        r = self._new(qty='8')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.data['number'], r.data['total'], r.data['still_due']), (1, '800.00', '800.00'))
        # shelf still says 10, but only 2 are free: a third reservation of 3 and a till sale of 3 are refused
        self.assertEqual(StockLevel.objects.get(variant=self.variant).quantity, D('10'))
        self.assertEqual(self._new(qty='3').data['code'], 'not_enough_stock')
        inv = self._draft()
        inv.items.update(quantity=D('3'))
        r = self._checkout(inv, self.cash)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('shortages', r.data)
        # 2 still sell
        inv.items.update(quantity=D('2'))
        self.assertEqual(self._checkout(inv, self.cash).status_code, 200)
        held = self.client.get(f'{URL}held/?branch={self.branch.id}').data
        self.assertEqual(held, {str(self.variant.id): '8.000'})

    # ── deposits + convert ─────────────────────────────────────────────────────────────────────────────────────
    def test_cash_deposit_counts_once_in_the_shift_and_convert_pays_the_rest(self):
        shift = self._shift()
        r = self._new(qty='3', deposit='100')
        self.assertEqual((r.status_code, r.data['deposit_held'], r.data['still_due']), (201, '100.00', '200.00'))
        rid = r.data['id']
        r = self.client.post(f'{URL}{rid}/deposit/', {'amount': '50', 'method': str(self.cash.id)}, format='json')
        self.assertEqual(r.data['deposit_held'], '150.00')
        self.assertEqual(shift.cash_in(), D('150'))
        # Ajel / store credit are not real money
        r = self.client.post(f'{URL}{rid}/deposit/', {'amount': '5', 'method': str(self.agel.id)}, format='json')
        self.assertEqual(r.data['code'], 'method_not_money')
        # convert: deposit 150 + 150 cash now = 300; stock leaves; the shift counts only the new 150
        r = self.client.post(f'{URL}{rid}/convert/', {'payments': [{'method': str(self.cash.id), 'amount': '150'}]},
                             format='json')
        self.assertEqual(r.status_code, 200, r.content)
        inv = SalesInvoice.objects.get(pk=r.data['id'])
        self.assertEqual((inv.status, inv.grand_total, inv.paid_amount), ('POSTED', D('300'), D('300')))
        self.assertEqual(StockLevel.objects.get(variant=self.variant).quantity, D('7'))
        self.assertEqual(Payment.objects.filter(invoice=inv, deposit_of__isnull=False).count(), 1)
        self.assertEqual(shift.cash_in(), D('300'))
        res = Reservation.objects.get(pk=rid)
        self.assertEqual((res.status, res.sale_id), ('CONVERTED', inv.id))
        self.assertEqual(customer_outstanding(self.customer), D('0'))
        # closed now: no more deposits, no second conversion, and the sale can't be voided (return instead)
        self.assertEqual(self.client.post(f'{URL}{rid}/convert/', {}, format='json').data['code'], 'not_open')
        r = self.mgr.post(f'/api/finance/invoices/{inv.id}/void/', {}, format='json')
        self.assertEqual(r.status_code, 400, r.content)

    def test_convert_rest_on_ajel_and_deposit_bigger_than_total_goes_to_store_credit(self):
        rid = self._new(qty='1', deposit='60').data['id']
        r = self.client.post(f'{URL}{rid}/convert/', {'payments': [{'method': str(self.agel.id), 'amount': '40'}]},
                             format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(customer_outstanding(self.customer), D('40'))
        # a reservation at 100 with 120 deposit → the sale is paid, 20 goes to store credit
        rid = self._new(qty='1', deposit='120').data['id']
        r = self.client.post(f'{URL}{rid}/convert/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.store_credit, D('20'))
        self.assertEqual(Reservation.objects.get(pk=rid).converted_extra_to_credit, D('20'))

    def test_serial_held_by_a_reservation_cannot_be_sold_elsewhere(self):
        from inventory.models import SerialUnit
        self.variant.product.track_serials = True
        self.variant.product.save(update_fields=['track_serials'])
        for sn in ('SN1', 'SN2'):
            SerialUnit.objects.create(store=self.store, variant=self.variant, branch=self.branch, serial=sn)
        body = {'branch': str(self.branch.id), 'customer': str(self.customer.id),
                'items': [{'variant': str(self.variant.id), 'quantity': '1', 'unit_price': '100',
                           'serial_numbers': ['sn1']}]}
        r = self.client.post(URL, body, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.client.post(URL, body, format='json').data['code'], 'serial_reserved')
        inv = self._draft()
        inv.items.update(serial_numbers=['SN1'])
        r = self._checkout(inv, self.cash)
        self.assertEqual((r.status_code, r.data.get('code')), (400, 'serial_reserved'), r.content)
        # a reservation of "a unit" without a serial: the serial is picked when it becomes a sale
        body['items'][0]['serial_numbers'] = []
        rid2 = self.client.post(URL, body, format='json').data['id']
        r = self.client.post(f'{URL}{rid2}/convert/', {'payments': [{'method': str(self.cash.id), 'amount': '100'}]},
                             format='json')
        self.assertEqual(r.status_code, 400, r.content)                       # no serial → the till's own refusal
        item2 = Reservation.objects.get(pk=rid2).items.get()
        r = self.client.post(f'{URL}{rid2}/convert/', {'payments': [{'method': str(self.cash.id), 'amount': '100'}],
                                                      'serials': {str(item2.id): ['sn1']}}, format='json')
        self.assertEqual(r.data.get('code'), 'serial_reserved', r.content)    # SN1 is the other reservation's
        r = self.client.post(f'{URL}{rid2}/convert/', {'payments': [{'method': str(self.cash.id), 'amount': '100'}],
                                                      'serials': {str(item2.id): ['sn2']}}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        # its own conversion sells it
        r = self.client.post(f"{URL}{Reservation.objects.get(status='OPEN').id}/convert/",
                             {'payments': [{'method': str(self.cash.id), 'amount': '100'}]}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(SerialUnit.objects.get(serial='SN1').status, 'SOLD')

    # ── cancel ─────────────────────────────────────────────────────────────────────────────────────────────────
    def test_cancel_refund_gives_cash_back_in_the_shift_and_frees_stock(self):
        shift = self._shift()
        rid = self._new(qty='10', deposit='200').data['id']
        r = self.client.post(f'{URL}{rid}/cancel/', {'outcome': 'REFUNDED'}, format='json')
        self.assertEqual(r.data['code'], 'reason_required')
        r = self.client.post(f'{URL}{rid}/cancel/', {'outcome': 'REFUNDED', 'reason': 'changed mind'}, format='json')
        self.assertEqual((r.status_code, r.data['status'], r.data['deposit_held']), (200, 'CANCELLED', '0.00'))
        self.assertEqual((shift.cash_in(), shift.cash_out()), (D('200'), D('200')))
        self.assertEqual(self._new(qty='10').status_code, 201)   # stock free again

    def test_cancel_keep_is_manager_only_and_counts_as_other_income(self):
        rid = self._new(qty='1', deposit='70').data['id']
        r = self.client.post(f'{URL}{rid}/cancel/', {'outcome': 'KEPT', 'reason': 'no show'}, format='json')
        self.assertEqual(r.status_code, 403)
        r = self.mgr.post(f'{URL}{rid}/cancel/', {'outcome': 'KEPT', 'reason': 'no show'}, format='json')
        self.assertEqual((r.status_code, r.data['kept_amount']), (200, '70.00'), r.content)
        today = timezone.localdate().isoformat()
        pnl = _client(self.owner).get(f'/api/reports/pnl/?date_from={today}&date_to={today}').data
        self.assertEqual(D(str(pnl['totals']['other_income'])), D('70'), pnl)

    def test_cancel_to_store_credit(self):
        rid = self._new(qty='1', deposit='45').data['id']
        r = self.client.post(f'{URL}{rid}/cancel/', {'outcome': 'TO_CREDIT', 'reason': 'later'}, format='json')
        self.assertEqual((r.status_code, r.data['deposit_outcome']), (200, 'TO_CREDIT'), r.content)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.store_credit, D('45'))

    def test_give_back_is_manager_only_and_capped(self):
        rid = self._new(qty='1', deposit='50').data['id']
        body = {'amount': '20', 'method': str(self.cash.id)}
        self.assertEqual(self.client.post(f'{URL}{rid}/give-back/', body, format='json').status_code, 403)
        self.assertEqual(self.mgr.post(f'{URL}{rid}/give-back/', {**body, 'amount': '60'},
                                       format='json').data['code'], 'more_than_deposit')
        r = self.mgr.post(f'{URL}{rid}/give-back/', body, format='json')
        self.assertEqual((r.status_code, r.data['deposit_held']), (200, '30.00'), r.content)

    # ── tenant ─────────────────────────────────────────────────────────────────────────────────────────────────
    def test_other_shop_cannot_see_or_touch(self):
        rid = self._new(qty='1').data['id']
        other_owner = User.objects.create_user(username='own_rs2', password='x', role='OWNER')
        other = Store.objects.create(name='Other', store_code='398', owner=other_owner)
        other_owner.store = other
        other_owner.save(update_fields=['store'])
        StoreSettings.objects.update_or_create(store=other, defaults={'reservations_enabled': True})
        c = _client(other_owner)
        self.assertEqual(c.get(URL).data['results'] if isinstance(c.get(URL).data, dict) else c.get(URL).data, [])
        self.assertEqual(c.post(f'{URL}{rid}/cancel/', {'outcome': 'KEPT', 'reason': 'x'},
                                format='json').status_code, 404)
        # and can't reserve this shop's product / customer
        r = c.post(URL, {'branch': str(self.branch.id), 'customer': str(self.customer.id),
                         'items': [{'variant': str(self.variant.id), 'quantity': '1', 'unit_price': '1'}]},
                   format='json')
        self.assertEqual(r.status_code, 400)


class ReservationTreasuryTests(ReservationTests):
    """The same flows with the Treasury on: deposits are box lines (cash → the drawer, card → its bank box), the sale
    made from a reservation writes no second line for the deposit."""
    TREASURY = True

    def test_deposit_lines_and_no_double_line_on_convert(self):
        rid = self._new(qty='2', deposit='100').data['id']
        self.client.post(f'{URL}{rid}/deposit/', {'amount': '50', 'method': str(self.card.id)}, format='json')
        self.assertEqual(ledger.balance(self.drawer), D('100'))
        self.assertEqual(ledger.balance(self.bank), D('50'))
        r = self.client.post(f'{URL}{rid}/convert/', {'payments': [{'method': str(self.cash.id), 'amount': '50'}]},
                             format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(ledger.balance(self.drawer), D('150'))   # + only the 50 paid now
        self.assertEqual(ledger.balance(self.bank), D('50'))
        kinds = sorted(MoneyMove.all_objects.filter(store=self.store).values_list('kind', flat=True))
        self.assertEqual(kinds, ['DEPOSIT_IN', 'DEPOSIT_IN', 'SALE_IN'])
        st = _client(self.owner).get(f'/api/treasury/boxes/{self.bank.id}/statement/')
        self.assertEqual(st.status_code, 200, st.content)
        refs = [m['ref'] for m in st.data['lines']]
        self.assertEqual(refs[0]['type'], 'reservation', st.data)

    def test_refund_on_cancel_leaves_the_drawer(self):
        rid = self._new(qty='1', deposit='80').data['id']
        r = self.client.post(f'{URL}{rid}/cancel/', {'outcome': 'REFUNDED', 'reason': 'x'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(ledger.balance(self.drawer), D('0'))
        self.assertEqual(MoneyMove.all_objects.filter(kind='DEPOSIT_OUT').count(), 1)
