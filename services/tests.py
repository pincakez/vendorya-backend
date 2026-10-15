from datetime import date
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store
from notifications.models import Notification
from services.models import Service
from users.models import User


class ServiceEditTests(TestCase):
    """§AUDIT A7 (s156): editing a job's cost or diagnosis built notifications with a field that does not
    exist (`recipient_id`) → the save went through, then the request crashed with a 500."""

    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_s', password='x', role='OWNER')
        self.store = Store.objects.create(name='Svc', store_code='103', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.manager = User.objects.create_user(username='mgr_s', password='x', role='MANAGER', store=self.store)
        self.job = Service.objects.create(store=self.store, client_name='Ali', receive_date=date.today(),
                                          cost=Decimal('100'), created_by=self.owner)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.manager).access_token))

    def test_cost_and_diagnosis_edit_saves_and_notifies(self):
        r = self.client.patch(f'/api/services/{self.job.id}/', {'cost': '250.00', 'diagnosis': 'Bad screen'}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.job.refresh_from_db()
        self.assertEqual(self.job.cost, Decimal('250.00'))
        self.assertTrue(Notification.objects.filter(store=self.store, title__contains='diagnosis & cost').exists())


class ServiceDonePaymentTests(TestCase):
    """Yakot 2026-10-06 picked (a): "Done" asks how the customer paid — like the till. Before, Done made an
    EMPTY invoice with paid=0, so every finished job became customer debt (§AUDIT A8)."""

    def setUp(self):
        from core.models import Branch, Address
        from finance.models import PaymentMethod
        from users.models import Customer
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_d', password='x', role='OWNER')
        self.store = Store.objects.create(name='Svc2', store_code='104', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        Branch.objects.create(store=self.store, name='Main', address=Address.objects.create(store=self.store, street_1='1', city='Cairo'))
        self.cashier = User.objects.create_user(username='csh_d', password='x', role='CASHIER', store=self.store)
        self.walkin = Customer.objects.create(store=self.store, name='Walk-in', phone_number='0', is_walk_in=True)
        self.client_row = Customer.objects.create(store=self.store, name='Ali', phone_number='0102')
        self.cash = PaymentMethod.objects.create(store=self.store, name='Cash', is_cash=True)
        self.agel = PaymentMethod.objects.create(store=self.store, name='Ajel', is_agel=True)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.cashier).access_token))

    def _job(self, cost='300', client=None):
        return Service.objects.create(store=self.store, client=client, client_name='Ali', receive_date=date.today(),
                                      cost=Decimal(cost), created_by=self.owner)

    def _done(self, job, method=None):
        body = {'method': str(method.id)} if method else {}
        return self.api.post(f'/api/services/{job.id}/done/', body, format='json')

    def test_cash_done_is_paid(self):
        job = self._job()
        r = self._done(job, self.cash)
        self.assertEqual(r.status_code, 200, r.content[:300])
        job.refresh_from_db()
        self.assertEqual(job.invoice.paid_amount, Decimal('300'))
        self.assertEqual(job.invoice.payments.get().created_by, self.cashier)

    def test_voiding_the_bill_reopens_the_job_and_it_bills_again(self):
        """s170: a job whose sale was voided stayed DONE with a VOID invoice and Done refused it forever."""
        from finance.models import SalesInvoice
        manager = User.objects.create_user(username='mgr_d', password='x', role='MANAGER', store=self.store)
        job = self._job()
        self.assertEqual(self._done(job, self.cash).status_code, 200)
        job.refresh_from_db()
        first = job.invoice
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(manager).access_token))
        r = api.post(f'/api/finance/invoices/{first.id}/void/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        job.refresh_from_db()
        self.assertEqual((job.status, job.invoice_id), (Service.Status.OPEN, None))
        self.assertIn(str(first.invoice_number), job.notes.get().body)
        self.assertEqual(self._done(job, self.cash).status_code, 200)
        job.refresh_from_db()
        self.assertNotEqual(job.invoice_id, first.id)
        self.assertEqual(SalesInvoice.objects.get(pk=first.pk).status, SalesInvoice.Status.VOID)

    def test_done_without_method_refused(self):
        job = self._job()
        self.assertEqual(self._done(job).status_code, 400)
        job.refresh_from_db()
        self.assertNotEqual(job.status, Service.Status.DONE)

    def test_ajel_done_becomes_named_customers_debt(self):
        from finance.models import customer_outstanding
        job = self._job(client=self.client_row)
        self.assertEqual(self._done(job, self.agel).status_code, 200)
        self.assertEqual(customer_outstanding(self.client_row), Decimal('300'))

    def test_ajel_done_needs_named_customer(self):
        self.assertEqual(self._done(self._job(), self.agel).status_code, 400)

    def test_free_job_needs_no_method(self):
        self.assertEqual(self._done(self._job(cost='0')).status_code, 200)


class ServiceDeskTests(TestCase):
    """PROLINE-PARITY Upgrade 13 — service desk: technician, device, parts from stock, a REAL invoice on Done
    (labour line + parts + tax, completed by the till's checkout), Returned refunds through the normal return."""

    def setUp(self):
        from core.models import Branch, Address, StoreSettings
        from finance.models import PaymentMethod
        from inventory.models import Supplier, Product, ProductVariant, StockLevel, Tax
        from users.models import Customer
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_k', password='x', role='OWNER')
        self.store = Store.objects.create(name='Fix', store_code='105', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.branch = Branch.objects.create(store=self.store, name='Main',
                                            address=Address.objects.create(store=self.store, street_1='1', city='Cairo'))
        self.cashier = User.objects.create_user(username='csh_k', password='x', role='CASHIER', store=self.store)
        self.manager = User.objects.create_user(username='mgr_k', password='x', role='MANAGER', store=self.store)
        self.tech = User.objects.create_user(username='tech_k', password='x', role='CASHIER', store=self.store)
        vat = Tax.objects.create(store=self.store, name='VAT', rate=Decimal('14'))
        st, _ = StoreSettings.objects.get_or_create(store=self.store)
        st.default_tax = vat
        st.save()
        sup = Supplier.objects.create(store=self.store, name='Sup', code_prefix='401', prefix_locked=True)
        self.part = ProductVariant.objects.create(
            product=Product.objects.create(store=self.store, name='SSD 512', supplier=sup),
            sell_price=Decimal('150'), cost_price=Decimal('90'))
        self.stock = StockLevel.objects.create(variant=self.part, branch=self.branch, quantity=Decimal('10'))
        self.walkin = Customer.objects.create(store=self.store, name='Walk-in', phone_number='0', is_walk_in=True)
        self.named = Customer.objects.create(store=self.store, name='Ali', phone_number='0103')
        self.cash = PaymentMethod.objects.create(store=self.store, name='Cash', is_cash=True)
        self.agel = PaymentMethod.objects.create(store=self.store, name='Ajel', is_agel=True)

        # another shop — its technician and product must never be usable here
        o2 = User.objects.create_user(username='own_k2', password='x', role='OWNER')
        self.store2 = Store.objects.create(name='Other', store_code='106', owner=o2)
        o2.store = self.store2
        o2.save(update_fields=['store'])
        self.tech2 = User.objects.create_user(username='tech_k2', password='x', role='CASHIER', store=self.store2)
        sup2 = Supplier.objects.create(store=self.store2, name='Sup2', code_prefix='402', prefix_locked=True)
        self.part2 = ProductVariant.objects.create(
            product=Product.objects.create(store=self.store2, name='Their SSD', supplier=sup2), sell_price=Decimal('1'))

    # ── helpers ──
    def _api(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
        return api

    def _job(self, client=None, parts=((2, '150'),), detection='100', cost='400', **extra):
        body = {'client': str(client.id) if client else None, 'client_name': 'Ali', 'receive_date': str(date.today()),
                'device': 'Dell Latitude 5420', 'device_serial': 'TAG-123', 'damage': 'No power',
                'detection_fee': detection, 'cost': cost, 'technician': self.tech.id,
                'parts': [{'variant': str(self.part.id), 'quantity': q, 'unit_price': p} for q, p in parts]}
        body.update(extra)
        r = self._api(self.cashier).post('/api/services/', body, format='json')
        self.assertEqual(r.status_code, 201, r.content[:400])
        return Service.objects.get(pk=r.data['id'])

    def _done(self, job, method=None, user=None):
        body = {'method': str(method.id)} if method else {}
        return self._api(user or self.cashier).post(f'/api/services/{job.id}/done/', body, format='json')

    def _stock(self):
        self.stock.refresh_from_db()
        return self.stock.quantity

    # ── s168: Done obeys "Require an open shift to sell" (it goes through the till's checkout) ──
    def test_done_needs_an_open_shift_when_the_shop_requires_one(self):
        from core.models import StoreSettings
        from finance.models import WorkShift
        st, _ = StoreSettings.objects.get_or_create(store=self.store)
        st.require_shift_to_sell = True
        st.save(update_fields=['require_shift_to_sell'])
        job = self._job()
        r = self._done(job, self.cash)
        self.assertEqual(r.status_code, 400, r.content[:400])
        self.assertEqual(r.data.get('code'), 'shift_required')
        job.refresh_from_db()
        self.assertEqual((job.status, job.invoice_id), (Service.Status.OPEN, None))   # nothing kept
        self.assertEqual(self._stock(), Decimal('10'))
        WorkShift.objects.create(store=self.store, user=self.cashier, branch=self.branch,
                                 starting_cash=Decimal('0'), status=WorkShift.Status.OPEN)
        self.assertEqual(self._done(job, self.cash).status_code, 200)

    # ── Done = a real invoice ──
    def test_done_bills_labour_parts_and_tax_and_moves_parts_stock(self):
        from finance.models import SalesInvoice
        from inventory.models import Product, StockLevel
        job = self._job()
        self.assertEqual(self._stock(), Decimal('10'))           # nothing leaves stock while the job is open
        r = self._done(job, self.cash)
        self.assertEqual(r.status_code, 200, r.content[:400])
        job.refresh_from_db()
        inv = job.invoice
        self.assertEqual((job.status, inv.status), (Service.Status.DONE, SalesInvoice.Status.POSTED))
        self.assertIsNotNone(inv.invoice_number)
        items = {i.variant.product.product_type: i for i in inv.items.select_related('variant__product')}
        labour, part = items[Product.ProductType.SERVICE], items[Product.ProductType.STANDARD]
        self.assertEqual((labour.quantity, labour.unit_price), (Decimal('1'), Decimal('500')))   # 100 + 400
        self.assertEqual((part.quantity, part.unit_price), (Decimal('2'), Decimal('150')))
        self.assertEqual((labour.tax_rate, labour.tax_amount), (Decimal('14'), Decimal('70')))   # tax snapshot
        self.assertEqual(part.tax_amount, Decimal('42'))
        self.assertEqual((inv.subtotal, inv.tax_total, inv.grand_total), (Decimal('800'), Decimal('112'), Decimal('912')))
        self.assertEqual(inv.paid_amount, Decimal('912'))
        self.assertEqual(part.cost_at_sale, Decimal('90'))                                      # COGS snapshot
        self.assertEqual(self._stock(), Decimal('8'))                                           # parts left once
        self.assertFalse(StockLevel.objects.filter(variant=labour.variant).exists())            # labour = no stock
        self.assertTrue(labour.variant.product.hide_from_pos)
        self.assertEqual(r.data['invoice_number'], inv.invoice_number)

    def test_done_twice_makes_one_invoice(self):
        from finance.models import SalesInvoice
        job = self._job()
        self.assertEqual(self._done(job, self.cash).status_code, 200)
        r = self._done(job, self.cash)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(SalesInvoice.objects.filter(store=self.store).count(), 1)
        self.assertEqual(self._stock(), Decimal('8'))

    def test_second_job_reuses_the_one_labour_product(self):
        from inventory.models import Product
        for _ in range(2):
            self.assertEqual(self._done(self._job(parts=()), self.cash).status_code, 200)
        self.assertEqual(Product.all_objects.filter(store=self.store, product_type=Product.ProductType.SERVICE).count(), 1)

    def test_ajel_without_named_customer_refused_and_nothing_kept(self):
        from finance.models import SalesInvoice
        job = self._job()
        self.assertEqual(self._done(job, self.agel).status_code, 400)
        job.refresh_from_db()
        self.assertEqual((job.status, job.invoice_id), (Service.Status.OPEN, None))
        self.assertEqual(SalesInvoice.all_objects.filter(store=self.store).count(), 0)   # the draft rolled back
        self.assertEqual(self._stock(), Decimal('10'))

    def test_charged_job_needs_a_payment_method(self):
        job = self._job()
        self.assertEqual(self._done(job).status_code, 400)
        job.refresh_from_db()
        self.assertEqual(job.status, Service.Status.OPEN)

    def test_split_payment_on_done(self):
        from finance.models import customer_outstanding
        job = self._job(client=self.named)
        r = self._api(self.cashier).post(f'/api/services/{job.id}/done/', {'payments': [
            {'method': str(self.cash.id), 'amount': '500'}, {'method': str(self.agel.id), 'amount': '1'}]}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(customer_outstanding(self.named), Decimal('412'))   # 912 − 500 cash, the rest on account

    def test_part_short_of_stock_refused_like_the_till(self):
        job = self._job(parts=((20, '150'),))
        r = self._done(job, self.cash)
        self.assertEqual(r.status_code, 400, r.content[:300])
        job.refresh_from_db()
        self.assertEqual(job.status, Service.Status.OPEN)
        self.assertEqual(self._stock(), Decimal('10'))

    def test_warranty_job_is_free_but_parts_leave_stock(self):
        job = self._job(is_warranty=True, warranty_days=90)
        r = self._done(job)                          # nothing to pay → no method needed
        self.assertEqual(r.status_code, 200, r.content[:300])
        job.refresh_from_db()
        self.assertEqual(job.invoice.grand_total, Decimal('0'))
        self.assertEqual(self._stock(), Decimal('8'))
        self.assertEqual(r.data['warranty_days'], 90)

    def test_charges_and_parts_frozen_after_done(self):
        job = self._job()
        self._done(job, self.cash)
        api = self._api(self.manager)
        r = api.patch(f'/api/services/{job.id}/', {'cost': '1'}, format='json')
        self.assertEqual(r.status_code, 400)
        r = api.patch(f'/api/services/{job.id}/', {'parts': []}, format='json')
        self.assertEqual(r.status_code, 400)
        r = api.patch(f'/api/services/{job.id}/', {'diagnosis': 'fan replaced'}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])

    def test_status_cannot_be_set_by_edit(self):
        job = self._job()
        self._api(self.manager).patch(f'/api/services/{job.id}/', {'status': 'DONE'}, format='json')
        job.refresh_from_db()
        self.assertEqual(job.status, Service.Status.OPEN)

    # ── Returned = the normal return ──
    def test_returned_paid_job_refunds_money_and_restocks_parts(self):
        from finance.models import RefundInvoice, customer_outstanding
        job = self._job(client=self.named)
        self._done(job, self.cash)
        self.assertEqual(self._api(self.cashier).post(f'/api/services/{job.id}/return/', {}, format='json').status_code, 403)
        r = self._api(self.manager).post(f'/api/services/{job.id}/return/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:400])
        job.refresh_from_db()
        self.assertEqual(job.status, Service.Status.RETURNED)
        refund = RefundInvoice.objects.get(original_invoice=job.invoice)
        self.assertEqual(job.refund, refund)
        self.assertEqual((refund.total_refunded, refund.paid_back, refund.drawer_cash),
                         (Decimal('912'), Decimal('912'), Decimal('912')))
        self.assertEqual(self._stock(), Decimal('10'))                                  # parts back on the shelf
        self.assertEqual(customer_outstanding(self.named), Decimal('0'))
        self.assertEqual(r.data['refund_number'], refund.refund_number)

    def test_returned_ajel_job_cancels_the_debt_and_pays_nothing_back(self):
        from finance.models import RefundInvoice, customer_outstanding
        job = self._job(client=self.named)
        self._done(job, self.agel)
        self.assertEqual(customer_outstanding(self.named), Decimal('912'))
        r = self._api(self.manager).post(f'/api/services/{job.id}/return/', {'restock_parts': False}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:400])
        job.refresh_from_db()
        refund = RefundInvoice.objects.get(original_invoice=job.invoice)
        self.assertEqual(refund.total_refunded, Decimal('912'))
        self.assertEqual((refund.paid_back, refund.drawer_cash), (Decimal('0'), Decimal('0')))
        self.assertEqual(customer_outstanding(self.named), Decimal('0'))
        self.assertEqual(self._stock(), Decimal('8'))                                   # dead part: not restocked

    def test_return_after_an_earlier_partial_return_refunds_only_the_rest(self):
        from finance.models import RefundInvoice
        job = self._job(client=self.named)
        self._done(job, self.cash)
        inv = Service.objects.get(pk=job.pk).invoice
        r = self._api(self.manager).post('/api/finance/refunds/', {
            'branch': str(self.branch.id), 'original_invoice': str(inv.id), 'customer': str(self.named.id),
            'items': [{'variant': str(self.part.id), 'quantity': '1', 'refund_amount': '171'}]}, format='json')
        self.assertEqual(r.status_code, 201, r.content[:400])
        r = self._api(self.manager).post(f'/api/services/{job.id}/return/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:400])
        total = sum(x.total_refunded for x in RefundInvoice.objects.filter(original_invoice=inv))
        self.assertEqual(total, Decimal('912'))
        self.assertEqual(self._stock(), Decimal('10'))

    def test_returning_the_labour_line_from_the_returns_screen_never_stocks_it(self):
        from inventory.models import StockLevel
        from services.desk import find_labour_variant
        job = self._job(client=self.named)
        self._done(job, self.cash)
        inv = Service.objects.get(pk=job.pk).invoice
        labour = find_labour_variant(self.store)
        r = self._api(self.manager).post('/api/finance/refunds/', {
            'branch': str(self.branch.id), 'original_invoice': str(inv.id), 'customer': str(self.named.id),
            'items': [{'variant': str(labour.id), 'quantity': '1', 'refund_amount': '1'}]}, format='json')
        self.assertEqual(r.status_code, 201, r.content[:400])
        self.assertFalse(StockLevel.objects.filter(variant=labour).exists())   # no "Low stock: Repair service" ever

    def test_a_serial_tracked_product_cannot_be_a_repair_part(self):
        self.part.product.track_serials = True
        self.part.product.save(update_fields=['track_serials'])
        body = {'client_name': 'X', 'receive_date': str(date.today()),
                'parts': [{'variant': str(self.part.id), 'quantity': '1'}]}
        r = self._api(self.cashier).post('/api/services/', body, format='json')
        self.assertEqual(r.status_code, 400, r.content[:300])
        self.assertIn('serial', str(r.content))

    # ── tenant + roles ──
    def test_technician_from_another_shop_refused(self):
        api = self._api(self.cashier)
        body = {'client_name': 'X', 'receive_date': str(date.today()), 'technician': self.tech2.id}
        self.assertEqual(api.post('/api/services/', body, format='json').status_code, 400)
        body['technician'] = self.tech.id
        self.assertEqual(api.post('/api/services/', body, format='json').status_code, 201)
        r = api.get(f'/api/services/?technician={self.tech.id}')
        rows = r.data['results'] if isinstance(r.data, dict) else r.data
        self.assertEqual(len(rows), 1)
        names = {u['name'] for u in api.get('/api/services/technicians/').data}
        self.assertIn('tech_k', names)
        self.assertNotIn('tech_k2', names)                     # never another shop's staff

    def test_part_from_another_shop_refused(self):
        body = {'client_name': 'X', 'receive_date': str(date.today()),
                'parts': [{'variant': str(self.part2.id), 'quantity': 1}]}
        self.assertEqual(self._api(self.cashier).post('/api/services/', body, format='json').status_code, 400)

    def test_part_price_defaults_to_sell_price_and_removal_soft_deletes(self):
        from services.models import ServicePart
        job = self._job(parts=())
        r = self._api(self.manager).patch(f'/api/services/{job.id}/', {
            'parts': [{'variant': str(self.part.id), 'quantity': 1}]}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(Decimal(r.data['parts'][0]['unit_price']), Decimal('150'))
        r = self._api(self.manager).patch(f'/api/services/{job.id}/', {'parts': []}, format='json')
        self.assertEqual(r.data['parts'], [])
        self.assertTrue(ServicePart.all_objects.get(service=job).is_deleted)

    def test_cashier_does_not_see_part_cost(self):
        job = self._job()
        r = self._api(self.cashier).get(f'/api/services/{job.id}/')
        self.assertNotIn('unit_cost', r.data['parts'][0])
        r = self._api(self.owner).get(f'/api/services/{job.id}/')
        self.assertEqual(Decimal(r.data['parts'][0]['unit_cost']), Decimal('90'))

    def test_notes_timeline(self):
        job = self._job()
        api = self._api(self.tech)
        self.assertEqual(api.post(f'/api/services/{job.id}/notes/', {'body': '  '}, format='json').status_code, 400)
        r = api.post(f'/api/services/{job.id}/notes/', {'body': 'Ordered a new fan'}, format='json')
        self.assertEqual(r.status_code, 201, r.content[:300])
        r = api.get(f'/api/services/{job.id}/')
        self.assertEqual([(n['body'], n['user_name']) for n in r.data['notes']], [('Ordered a new fan', 'tech_k')])

    # ── review fixes (s168): taxed shop — quote = invoice = what the drawer takes ──
    def test_taxed_shop_quote_equals_invoice_and_drawer_takes_the_taxed_total(self):
        """Shop with 14% tax, repair 1000: the window must say 1140 and the drawer must expect 1140."""
        job = self._job(parts=(), detection='0', cost='1000')
        q = self._api(self.cashier).get(f'/api/services/{job.id}/quote/')
        self.assertEqual(q.status_code, 200, q.content[:300])
        self.assertEqual((Decimal(q.data['subtotal']), Decimal(q.data['tax_total']), Decimal(q.data['grand_total'])),
                         (Decimal('1000'), Decimal('140'), Decimal('1140')))
        self.assertEqual(Decimal(q.data['lines'][0]['tax_rate']), Decimal('14'))
        api = self._api(self.cashier)
        r = api.post('/api/finance/shifts/', {'branch': str(self.branch.id), 'starting_cash': '0'}, format='json')
        self.assertEqual(r.status_code, 201, r.content[:300])
        r = api.post(f'/api/services/{job.id}/done/', {'method': str(self.cash.id),
                                                      'expected_total': q.data['grand_total']}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        job.refresh_from_db()
        self.assertEqual((job.invoice.grand_total, job.invoice.paid_amount), (Decimal('1140'), Decimal('1140')))
        d = api.get('/api/finance/shifts/drawer/').data
        self.assertEqual(Decimal(d['cash_in']), Decimal('1140'))

    def test_quote_with_parts_matches_the_invoice(self):
        job = self._job()
        q = self._api(self.cashier).get(f'/api/services/{job.id}/quote/').data
        self._done(job, self.cash)
        job.refresh_from_db()
        inv = job.invoice
        self.assertEqual((Decimal(q['subtotal']), Decimal(q['tax_total']), Decimal(q['grand_total'])),
                         (inv.subtotal, inv.tax_total, inv.grand_total))

    def test_done_refused_when_the_shown_total_is_stale(self):
        from finance.models import SalesInvoice
        job = self._job()
        r = self._api(self.cashier).post(f'/api/services/{job.id}/done/',
                                         {'method': str(self.cash.id), 'expected_total': '800'}, format='json')
        self.assertEqual(r.status_code, 409, r.content[:300])
        job.refresh_from_db()
        self.assertEqual(job.status, Service.Status.OPEN)
        self.assertEqual(SalesInvoice.all_objects.filter(store=self.store).count(), 0)
        self.assertEqual(self._stock(), Decimal('10'))

    def test_owner_sets_the_labour_tax_and_can_edit_the_labour_product(self):
        from inventory.models import Tax
        zero = Tax.objects.create(store=self.store, name='Exempt', rate=Decimal('0'))
        other_shop_tax = Tax.objects.create(store=self.store2, name='X', rate=Decimal('5'))
        owner = self._api(self.owner)
        self.assertEqual(owner.patch('/api/services/labour/', {'tax': str(other_shop_tax.id)}, format='json').status_code, 400)
        self.assertEqual(self._api(self.cashier).patch('/api/services/labour/', {'tax': None}, format='json').status_code, 403)
        r = owner.patch('/api/services/labour/', {'tax': str(zero.id), 'name': 'Labour'}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        # Opening + saving the SKU-less, supplier-less system product in Products must not crash (no SKU needed).
        r = owner.patch(f"/api/inventory/products/{r.data['product']}/", {'name': 'Labour & repair'}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        job = self._job()                                   # labour 500 (exempt) + parts 300 (14% = 42)
        self.assertEqual(Decimal(self._api(self.cashier).get(f'/api/services/{job.id}/quote/').data['grand_total']),
                         Decimal('842'))
        self.assertEqual(self._done(job, self.cash).status_code, 200)
        job.refresh_from_db()
        self.assertEqual((job.invoice.tax_total, job.invoice.grand_total), (Decimal('42'), Decimal('842')))

    def test_return_after_an_earlier_return_in_a_pack_unit(self):
        """A part sold in base units, 1 Pack (= 2) returned at the till first; the job's return takes the rest."""
        from finance.models import RefundInvoice
        from inventory.models import ProductUnit
        pack = ProductUnit.objects.create(variant=self.part, name='Pack', factor=Decimal('2'), sell_price=Decimal('300'))
        job = self._job(client=self.named, parts=((4, '150'),))     # labour 570 incl. tax + parts 684 = 1254
        self._done(job, self.cash)
        inv = Service.objects.get(pk=job.pk).invoice
        self.assertEqual(inv.grand_total, Decimal('1254'))
        r = self._api(self.manager).post('/api/finance/refunds/', {
            'branch': str(self.branch.id), 'original_invoice': str(inv.id), 'customer': str(self.named.id),
            'items': [{'variant': str(self.part.id), 'unit': str(pack.id), 'quantity': '1', 'refund_amount': '342'}]},
            format='json')
        self.assertEqual(r.status_code, 201, r.content[:400])
        self.assertEqual(self._stock(), Decimal('8'))                  # 10 − 4 + 2
        r = self._api(self.manager).post(f'/api/services/{job.id}/return/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:400])
        job.refresh_from_db()
        part_line = job.refund.items.get(variant=self.part)
        self.assertEqual((part_line.quantity * part_line.unit_factor, part_line.refund_amount), (Decimal('2'), Decimal('342')))
        total = sum(x.total_refunded for x in RefundInvoice.objects.filter(original_invoice=inv))
        self.assertEqual(total, Decimal('1254'))
        self.assertEqual(self._stock(), Decimal('10'))


class ServiceDeviceSerialTests(TestCase):
    """s169 — a job's device serial looks up the shop's OWN serial unit: product, the sale it left on, its
    warranty. Never another shop's unit; an unknown serial stays plain text (nulls, no error)."""

    setUp = ServiceDeskTests.setUp     # same two shops, users and stock
    _api = ServiceDeskTests._api

    def _unit(self, store, serial, sold_days_ago=0, warranty_ends_on=None, status='SOLD'):
        from datetime import timedelta
        from django.utils import timezone
        from core.models import Branch, Address
        from finance.models import SalesInvoice, SalesInvoiceItem
        from inventory.models import Product, ProductVariant, SerialUnit, Supplier
        from users.models import Customer
        sup = Supplier.objects.filter(store=store).first()
        product = Product.objects.create(store=store, name=f'Laptop {serial}', track_serials=True, supplier=sup)
        variant = ProductVariant.objects.create(product=product, sell_price=Decimal('20000'))
        branch = Branch.objects.filter(store=store).first() or Branch.objects.create(
            store=store, name='B', address=Address.objects.create(store=store, street_1='2', city='Giza'))
        item = None
        if status == 'SOLD':
            cust = Customer.objects.create(store=store, name='Buyer', phone_number='0109')
            inv = SalesInvoice.objects.create(store=store, branch=branch, customer=cust, invoice_number=7001,
                                              date=timezone.now() - timedelta(days=sold_days_ago))
            item = SalesInvoiceItem.objects.create(invoice=inv, variant=variant, quantity=1,
                                                   unit_price=Decimal('20000'), total=Decimal('20000'))
        return SerialUnit.all_objects.create(store=store, variant=variant, branch=branch, serial=serial,
                                             status=status, sale_item=item, warranty_ends_on=warranty_ends_on,
                                             warranty_months=12 if warranty_ends_on else 0)

    def _job(self, serial):
        r = self._api(self.cashier).post('/api/services/', {
            'client_name': 'Ali', 'receive_date': str(date.today()), 'service_type': 'Repair',
            'device_serial': serial}, format='json')
        self.assertEqual(r.status_code, 201, r.content[:400])
        return r.json()

    def _list_row(self, job_id):
        data = self._api(self.cashier).get('/api/services/').json()
        return next(r for r in (data['results'] if isinstance(data, dict) else data) if r['id'] == job_id)

    def _lookup(self, serial):
        r = self._api(self.cashier).get('/api/services/device-lookup/', {'serial': serial})
        self.assertEqual(r.status_code, 200, r.content[:400])
        return r.json()['unit']

    def test_match_in_same_shop_fills_product_sale_and_running_warranty(self):
        from datetime import timedelta
        unit = self._unit(self.store, 'ABC123', sold_days_ago=30, warranty_ends_on=date.today() + timedelta(days=300))
        job = self._job(' abc 123 ')          # typed with spaces + lower case → the same unit
        du = job['device_unit']
        self.assertEqual(du['id'], str(unit.id))
        self.assertEqual(du['product_id'], str(unit.variant.product_id))
        self.assertEqual(du['product_name'], 'Laptop ABC123')
        self.assertEqual(du['sale']['id'], str(unit.sale_item.invoice_id))
        self.assertEqual(du['sale']['invoice_number'], 7001)
        self.assertTrue(du['sale']['date'])
        self.assertEqual(du['warranty_ends_on'], str(unit.warranty_ends_on))
        self.assertTrue(du['warranty_active'])
        # the list (one batched lookup), the job page and the form's lookup-while-typing all say the same
        self.assertEqual(self._list_row(job['id'])['device_unit'], du)
        self.assertEqual(self._api(self.cashier).get(f"/api/services/{job['id']}/").json()['device_unit'], du)
        self.assertEqual(self._lookup('abc123'), du)

    def test_warranty_ended(self):
        from datetime import timedelta
        self._unit(self.store, 'OLD1', sold_days_ago=500, warranty_ends_on=date.today() - timedelta(days=1))
        du = self._job('OLD1')['device_unit']
        self.assertEqual(du['warranty_ends_on'], str(date.today() - timedelta(days=1)))
        self.assertFalse(du['warranty_active'])
        self.assertIsNotNone(du['sale'])

    def test_warranty_ending_today_still_runs_and_a_unit_on_the_shelf_has_no_sale(self):
        self._unit(self.store, 'TODAY1', warranty_ends_on=date.today())
        self.assertTrue(self._job('TODAY1')['device_unit']['warranty_active'])
        self._unit(self.store, 'SHELF1', status='IN_STOCK')
        du = self._job('SHELF1')['device_unit']
        self.assertEqual((du['status'], du['sale'], du['warranty_ends_on'], du['warranty_active']),
                         ('IN_STOCK', None, None, False))

    def test_other_shops_unit_is_never_shown(self):
        from datetime import timedelta
        self._unit(self.store2, 'THEIRS9', warranty_ends_on=date.today() + timedelta(days=100))
        job = self._job('THEIRS9')
        self.assertIsNone(job['device_unit'])
        self.assertIsNone(self._list_row(job['id'])['device_unit'])
        self.assertIsNone(self._lookup('THEIRS9'))

    def test_unknown_or_removed_serial_is_null_not_an_error(self):
        job = self._job('NOPE-42')
        self.assertEqual(job['device_serial'], 'NOPE-42')      # free text kept as typed
        self.assertIsNone(job['device_unit'])
        self.assertIsNone(self._list_row(job['id'])['device_unit'])
        self.assertIsNone(self._job('')['device_unit'])
        unit = self._unit(self.store, 'GONE1', status='IN_STOCK')
        unit.delete()                                          # soft-removed (a wrongly typed serial)
        self.assertIsNone(self._job('GONE1')['device_unit'])
        for q in ('NOPE-42', '', '   '):
            self.assertIsNone(self._lookup(q))
