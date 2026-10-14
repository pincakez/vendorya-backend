"""Serial / IMEI per unit + condition + warranty (PROLINE-PARITY New 7 + New 8).

Every stock path that moves a serial-tracked product carries its serials: purchase receive (in), till
checkout (out, locked), refund (back), void (back), adjustment (±), transfer (branch → branch). Plus the
lookup API, the POS scan, the receipt data and the "register serials on existing stock" tool.

Run: DB_TEST_NAME=test_vendorya_serials venv/bin/python manage.py test inventory.test_serials --noinput
"""
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from core.models import Store, StoreSettings, Branch, Address
from finance.models import (
    PaymentMethod, PurchaseInvoice, SalesInvoice, SalesInvoiceItem, RefundInvoice,
)
from inventory.models import (
    Supplier, Product, ProductVariant, StockLevel, SerialUnit, SerialMove, StockAdjustment,
)
from inventory import serials as serial_engine
from users.models import User, Customer


def _client_for(user):
    from rest_framework.test import APIClient
    from rest_framework_simplejwt.tokens import RefreshToken
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(user).access_token))
    return c


class _SerialBase(TestCase):

    def setUp(self):
        from django.conf import settings as dj_settings
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='own_sn', password='x', role='OWNER')
        self.store = Store.objects.create(name='Laptops', store_code='131', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        self.manager = User.objects.create_user(username='mgr_sn', password='x', role='MANAGER', store=self.store)
        self.cashier = User.objects.create_user(username='csh_sn', password='x', role='CASHIER', store=self.store)
        StoreSettings.objects.get_or_create(store=self.store)
        addr = Address.objects.create(store=self.store, street_1='1', city='Cairo')
        self.branch = Branch.objects.create(store=self.store, name='Main', address=addr)
        addr2 = Address.objects.create(store=self.store, street_1='2', city='Cairo')
        self.branch2 = Branch.objects.create(store=self.store, name='Mall', address=addr2)
        self.supplier = Supplier.objects.create(store=self.store, name='Dist', code_prefix='431', prefix_locked=True)
        self.product = Product.objects.create(
            store=self.store, name='Laptop X1', supplier=self.supplier, track_serials=True,
            condition=Product.Condition.IMPORTED_INSPECTED, warranty_months=12)
        self.variant = ProductVariant.objects.create(product=self.product, sell_price=Decimal('20000'))
        self.customer = Customer.objects.create(store=self.store, name='Buyer', phone_number='0101')
        self.cash = PaymentMethod.objects.create(store=self.store, name='Cash', is_cash=True)
        self.mgr = _client_for(self.manager)
        self.till = _client_for(self.cashier)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _purchase(self, qty, serials, branch=None, variant=None):
        r = self.mgr.post('/api/finance/purchases/', {
            'supplier': str(self.supplier.id), 'branch': str((branch or self.branch).id),
            'date': timezone.now().isoformat(),
            'lines': [{'variant': str((variant or self.variant).id), 'quantity': str(qty),
                       'base_price': '15000', 'serial_numbers': serials}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return r.data['id']

    def _receive(self, pid):
        return self.mgr.post(f'/api/finance/purchases/{pid}/receive/', {}, format='json')

    def _stock_in(self, serials, branch=None):
        r = self._receive(self._purchase(len(serials), serials, branch))
        self.assertEqual(r.status_code, 200, r.content)

    def _cart(self, serials, qty=None, branch=None, client=None, variant=None):
        r = (client or self.till).post('/api/finance/invoices/', {
            'branch': str((branch or self.branch).id), 'customer': str(self.customer.id),
            'status': 'DRAFT', 'date': timezone.now().isoformat(),
            'items': [{'variant': str((variant or self.variant).id),
                       'quantity': str(qty if qty is not None else len(serials)),
                       'unit_price': '20000', 'serial_numbers': serials}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return r.data['id']

    def _checkout(self, inv_id, client=None):
        return (client or self.till).post(f'/api/finance/invoices/{inv_id}/checkout/',
                                          {'method': str(self.cash.id)}, format='json')

    def _sell(self, serials, **kw):
        inv_id = self._cart(serials, **kw)
        r = self._checkout(inv_id)
        self.assertEqual(r.status_code, 200, r.content)
        return inv_id

    def _stock(self, branch=None):
        sl = StockLevel.objects.filter(variant=self.variant, branch=branch or self.branch).first()
        return sl.quantity if sl else Decimal('0')

    def _unit(self, serial):
        return SerialUnit.all_objects.filter(store=self.store, serial=serial).order_by('-created_at').first()


class PurchaseReceiveSerialTests(_SerialBase):

    def test_receive_creates_one_unit_per_serial(self):
        self._stock_in(['sn-001', ' SN 002 ', 'Sn003'])
        self.assertEqual(self._stock(), Decimal('3'))
        units = SerialUnit.all_objects.filter(store=self.store, status='IN_STOCK').order_by('serial')
        self.assertEqual([u.serial for u in units], ['SN-001', 'SN002', 'SN003'])   # normalised
        self.assertTrue(all(u.branch_id == self.branch.id and u.purchase_item_id for u in units))
        self.assertEqual(SerialMove.all_objects.filter(kind='RECEIVED').count(), 3)

    def test_receive_count_mismatch_refused_and_nothing_moves(self):
        pid = self._purchase(3, ['A1', 'A2'])
        r = self._receive(pid)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_count')
        self.assertEqual(PurchaseInvoice.objects.get(pk=pid).status, 'DRAFT')
        self.assertEqual(self._stock(), Decimal('0'))
        self.assertFalse(SerialUnit.all_objects.exists())

    def test_receive_serial_already_on_shelf_refused(self):
        self._stock_in(['DUP1'])
        r = self._receive(self._purchase(1, ['dup1']))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_in_stock')
        self.assertEqual(self._stock(), Decimal('1'))

    def test_serial_typed_twice_refused(self):
        r = self.mgr.post('/api/finance/purchases/', {
            'supplier': str(self.supplier.id), 'branch': str(self.branch.id), 'date': timezone.now().isoformat(),
            'lines': [{'variant': str(self.variant.id), 'quantity': '2', 'base_price': '1',
                       'serial_numbers': 'X9\nx9'}]}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_duplicate')

    def test_pasted_column_accepted(self):
        pid = self._purchase(2, 'P-1\r\nP-2\n')
        self.assertEqual(self._receive(pid).status_code, 200)
        self.assertEqual(SerialUnit.all_objects.filter(status='IN_STOCK').count(), 2)

    def test_untracked_product_ignores_serials(self):
        plain = Product.objects.create(store=self.store, name='Mouse', supplier=self.supplier)
        pv = ProductVariant.objects.create(product=plain, sell_price=Decimal('100'))
        pid = self._purchase(2, ['Z1'], variant=pv)
        self.assertEqual(self._receive(pid).status_code, 200)
        self.assertFalse(SerialUnit.all_objects.exists())


class SaleSerialTests(_SerialBase):

    def setUp(self):
        super().setUp()
        self._stock_in(['L-1', 'L-2', 'L-3'])

    def test_sale_marks_units_sold_with_warranty_and_condition(self):
        inv_id = self._sell(['l-1', 'L-2'])
        self.assertEqual(self._stock(), Decimal('1'))
        inv = SalesInvoice.objects.get(pk=inv_id)
        item = inv.items.get()
        self.assertEqual(item.serial_numbers, ['L-1', 'L-2'])
        self.assertEqual(item.condition, 'IMPORTED_INSPECTED')
        self.assertEqual(item.warranty_months, 12)
        sale_day = timezone.localdate(inv.date)
        expected = serial_engine.add_months(sale_day, 12)
        self.assertEqual(item.warranty_ends_on, expected)
        for s in ('L-1', 'L-2'):
            u = self._unit(s)
            self.assertEqual((u.status, u.sale_item_id, u.warranty_ends_on), ('SOLD', item.id, expected))
        self.assertEqual(self._unit('L-3').status, 'IN_STOCK')

    def test_same_serial_cannot_be_sold_twice(self):
        first = self._cart(['L-1'])
        second = self._cart(['L-1'])
        self.assertEqual(self._checkout(first).status_code, 200)
        r = self._checkout(second)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_not_available')
        self.assertEqual(SalesInvoice.objects.get(pk=second).status, 'DRAFT')
        self.assertEqual(self._stock(), Decimal('2'))     # only the first sale took stock

    def test_serial_at_other_branch_refused(self):
        StockLevel.objects.create(variant=self.variant, branch=self.branch2, quantity=Decimal('5'))
        r = self._checkout(self._cart(['L-1'], branch=self.branch2))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_not_available')
        self.assertEqual(self._unit('L-1').status, 'IN_STOCK')

    def test_other_shops_serial_refused(self):
        other_owner = User.objects.create_user(username='own_o', password='x', role='OWNER')
        other = Store.objects.create(name='Other', store_code='132', owner=other_owner)
        oaddr = Address.objects.create(store=other, street_1='2', city='Giza')
        obranch = Branch.objects.create(store=other, name='O', address=oaddr)
        osup = Supplier.objects.create(store=other, name='OS', code_prefix='432', prefix_locked=True)
        oprod = Product.objects.create(store=other, name='Their laptop', track_serials=True, supplier=osup)
        ov = ProductVariant.objects.create(product=oprod, sell_price=Decimal('1'))
        SerialUnit.all_objects.create(store=other, variant=ov, branch=obranch, serial='THEIRS1')
        r = self._checkout(self._cart(['THEIRS1']))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_not_available')
        self.assertEqual(SerialUnit.all_objects.get(serial='THEIRS1').status, 'IN_STOCK')

    def test_count_must_match_quantity(self):
        r = self._checkout(self._cart(['L-1'], qty=2))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_count')
        self.assertEqual(self._stock(), Decimal('3'))

    def test_same_serial_on_two_lines_refused(self):
        r = self.till.post('/api/finance/invoices/', {
            'branch': str(self.branch.id), 'customer': str(self.customer.id), 'status': 'DRAFT',
            'date': timezone.now().isoformat(),
            'items': [{'variant': str(self.variant.id), 'quantity': '1', 'unit_price': '1', 'serial_numbers': ['L-1']},
                      {'variant': str(self.variant.id), 'quantity': '1', 'unit_price': '1', 'serial_numbers': ['L-1']}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        r = self._checkout(r.data['id'])
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_duplicate')

    def test_pack_unit_refused(self):
        from inventory.models import ProductUnit
        box = ProductUnit.objects.create(variant=self.variant, name='Box', factor=Decimal('2'))
        inv_id = self._cart(['L-1', 'L-2'], qty=1)
        SalesInvoiceItem.objects.filter(invoice_id=inv_id).update(unit=box, unit_factor=Decimal('2'))
        r = self._checkout(inv_id)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_unit')

    def test_void_puts_serials_back(self):
        inv_id = self._sell(['L-1'])
        r = self.mgr.post(f'/api/finance/invoices/{inv_id}/void/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        u = self._unit('L-1')
        self.assertEqual((u.status, u.sale_item_id, u.warranty_ends_on), ('IN_STOCK', None, None))
        self.assertEqual(self._stock(), Decimal('3'))
        self.assertTrue(SerialMove.all_objects.filter(unit=u, kind='VOIDED').exists())

    def test_receipt_data_carries_serials_condition_warranty(self):
        inv_id = self._sell(['L-3'])
        d = self.till.get(f'/api/finance/invoices/{inv_id}/print-data/').data
        line = d['items'][0]
        self.assertEqual(line['serial_numbers'], ['L-3'])
        self.assertEqual(line['condition'], 'IMPORTED_INSPECTED')
        self.assertEqual(line['warranty_months'], 12)
        self.assertTrue(line['warranty_ends_on'])

    def test_untracked_line_gets_warranty_snapshot(self):
        plain = Product.objects.create(store=self.store, name='Bag', supplier=self.supplier,
                                       condition=Product.Condition.NEW, warranty_months=6)
        pv = ProductVariant.objects.create(product=plain, sell_price=Decimal('100'))
        StockLevel.objects.create(variant=pv, branch=self.branch, quantity=Decimal('4'))
        inv_id = self._sell([], qty=1, variant=pv)
        item = SalesInvoice.objects.get(pk=inv_id).items.get()
        self.assertEqual((item.condition, item.warranty_months, item.serial_numbers), ('NEW', 6, []))
        self.assertEqual(item.warranty_ends_on, serial_engine.add_months(timezone.localdate(item.invoice.date), 6))
        # editing the product later never rewrites the sold line
        plain.warranty_months = 24
        plain.save()
        item.refresh_from_db()
        self.assertEqual(item.warranty_months, 6)


class RefundSerialTests(_SerialBase):

    def setUp(self):
        super().setUp()
        self._stock_in(['R-1', 'R-2', 'R-3'])
        self.inv_id = self._sell(['R-1', 'R-2'])

    def _refund(self, serials, qty=None, restock=True, original=True):
        body = {'branch': str(self.branch.id), 'customer': str(self.customer.id),
                'items': [{'variant': str(self.variant.id), 'quantity': str(qty or len(serials)),
                           'refund_amount': '1', 'restock_inventory': restock, 'serial_numbers': serials}]}
        if original:
            body['original_invoice'] = self.inv_id
        return self.mgr.post('/api/finance/refunds/', body, format='json')

    def test_refund_puts_serial_back_in_stock(self):
        r = self._refund(['r-1'])
        self.assertEqual(r.status_code, 201, r.content)
        u = self._unit('R-1')
        self.assertEqual((u.status, u.sale_item_id, u.branch_id), ('IN_STOCK', None, self.branch.id))
        self.assertEqual(self._unit('R-2').status, 'SOLD')
        self.assertEqual(self._stock(), Decimal('2'))
        self.assertTrue(SerialMove.all_objects.filter(unit=u, kind='RETURNED').exists())
        # sellable again
        self._sell(['R-1'])
        self.assertEqual(self._unit('R-1').status, 'SOLD')

    def test_refund_serial_not_on_that_sale_refused(self):
        r = self._refund(['R-3'])     # in stock, never sold
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_not_sold')
        self.assertFalse(RefundInvoice.objects.exists())
        self.assertEqual(self._stock(), Decimal('1'))

    def test_refund_serial_sold_on_another_invoice_refused(self):
        other_inv = self._sell(['R-3'])
        r = self._refund(['R-3'])
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(self._unit('R-3').status, 'SOLD')
        self.assertTrue(other_inv)

    def test_refund_without_restock_keeps_unit_aside(self):
        r = self._refund(['R-2'], restock=False)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self._unit('R-2').status, 'RETURNED')
        self.assertEqual(self._stock(), Decimal('1'))

    def test_refund_count_mismatch_refused(self):
        r = self._refund(['R-1'], qty=2)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_count')


class AdjustTransferRegisterTests(_SerialBase):

    def test_adjustment_gain_and_loss_by_serial(self):
        r = self.mgr.post('/api/inventory/adjustments/', {
            'variant': str(self.variant.id), 'branch': str(self.branch.id), 'quantity_change': '2',
            'reason': 'OPENING', 'serial_numbers': ['A-1', 'A-2']}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self._stock(), Decimal('2'))
        r = self.mgr.post('/api/inventory/adjustments/', {
            'variant': str(self.variant.id), 'branch': str(self.branch.id), 'quantity_change': '-1',
            'reason': 'THEFT', 'serial_numbers': ['A-2']}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self._unit('A-2').status, 'WRITTEN_OFF')
        self.assertEqual(self._stock(), Decimal('1'))

    def test_adjustment_without_serials_refused(self):
        r = self.mgr.post('/api/inventory/adjustments/', {
            'variant': str(self.variant.id), 'branch': str(self.branch.id), 'quantity_change': '1',
            'reason': 'OPENING'}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(self._stock(), Decimal('0'))
        self.assertFalse(StockAdjustment.all_objects.exists())

    def test_transfer_moves_units(self):
        self._stock_in(['T-1', 'T-2'])
        admin = User.objects.create_user(username='adm_sn', password='x', role='ADMIN', store=self.store)
        r = _client_for(admin).post('/api/inventory/transfers/', {
            'from_branch': str(self.branch.id), 'to_branch': str(self.branch2.id),
            'items': [{'variant': str(self.variant.id), 'quantity': '1', 'serial_numbers': ['T-2']}]}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self._unit('T-2').branch_id, self.branch2.id)
        self.assertEqual((self._stock(), self._stock(self.branch2)), (Decimal('1'), Decimal('1')))
        r = _client_for(admin).post('/api/inventory/transfers/', {
            'from_branch': str(self.branch.id), 'to_branch': str(self.branch2.id),
            'items': [{'variant': str(self.variant.id), 'quantity': '1', 'serial_numbers': ['T-2']}]}, format='json')
        self.assertEqual(r.status_code, 400, r.content)      # T-2 is no longer at Main
        self.assertEqual(self._stock(), Decimal('1'))

    def test_register_is_capped_by_untracked_stock(self):
        StockLevel.objects.create(variant=self.variant, branch=self.branch, quantity=Decimal('2'))
        r = self.mgr.post('/api/inventory/serials/register/', {
            'variant': str(self.variant.id), 'branch': str(self.branch.id),
            'serial_numbers': ['G-1', 'G-2', 'G-3']}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_register_cap')
        r = self.mgr.post('/api/inventory/serials/register/', {
            'variant': str(self.variant.id), 'branch': str(self.branch.id),
            'serial_numbers': 'G-1\nG-2'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self._stock(), Decimal('2'))          # never moves stock
        r = self.mgr.post('/api/inventory/serials/register/', {
            'variant': str(self.variant.id), 'branch': str(self.branch.id), 'serial_numbers': ['G-9']}, format='json')
        self.assertEqual(r.status_code, 400, r.content)        # all on-hand units already have a serial
        # a cashier can't register
        r = self.till.post('/api/inventory/serials/register/', {
            'variant': str(self.variant.id), 'branch': str(self.branch.id), 'serial_numbers': ['G-9']}, format='json')
        self.assertEqual(r.status_code, 403)
        # remove a typo → the room comes back
        uid = self._unit('G-2').id
        self.assertEqual(self.mgr.post(f'/api/inventory/serials/{uid}/remove/', {}, format='json').status_code, 204)
        r = self.mgr.post('/api/inventory/serials/register/', {
            'variant': str(self.variant.id), 'branch': str(self.branch.id), 'serial_numbers': ['G-2B']}, format='json')
        self.assertEqual(r.status_code, 201, r.content)

    def test_storage_refuses_tracked_product(self):
        from django.core.exceptions import ValidationError
        from inventory.models import StorageLocation
        from inventory.storage_service import move_to_storage
        self._stock_in(['S-1'])
        loc = StorageLocation.objects.create(store=self.store, name='Back')
        with self.assertRaises(ValidationError):
            move_to_storage(variant=self.variant, qty=1, from_branch=self.branch, storage_location=loc,
                            user=self.manager)

    def test_switching_tracking_off_refused_while_units_on_shelf(self):
        self._stock_in(['O-1'])
        r = self.mgr.patch(f'/api/inventory/products/{self.product.id}/', {'track_serials': False}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.product.refresh_from_db()
        self.assertTrue(self.product.track_serials)


class LookupAndScanTests(_SerialBase):

    def setUp(self):
        super().setUp()
        self._stock_in(['IMEI-111', 'IMEI-222'])
        self.inv_id = self._sell(['IMEI-111'])

    def test_lookup_shows_purchase_sale_warranty_and_history(self):
        r = self.till.get('/api/inventory/serials/', {'q': 'imei-1'})
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.data['results']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['serial'], row['status'], row['product_name']), ('IMEI-111', 'SOLD', 'Laptop X1'))
        self.assertEqual(row['sale']['customer_name'], 'Buyer')
        self.assertEqual(row['purchase']['supplier_name'], 'Dist')
        self.assertTrue(row['warranty_active'])
        self.assertNotIn('cost', str(row))
        d = self.till.get(f"/api/inventory/serials/{row['id']}/").data
        self.assertEqual([m['kind'] for m in d['moves']], ['RECEIVED', 'SOLD'])

    def test_lookup_is_scoped_to_the_shop(self):
        other_owner = User.objects.create_user(username='own_o2', password='x', role='OWNER')
        other = Store.objects.create(name='Other2', store_code='133', owner=other_owner)
        u = self._unit('IMEI-222')
        r = _client_for(other_owner).get('/api/inventory/serials/', {'q': 'IMEI'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['results'], [])
        self.assertEqual(_client_for(other_owner).get(f'/api/inventory/serials/{u.id}/').status_code, 404)
        self.assertTrue(other)

    def test_find_serial_helper(self):
        self.assertEqual(serial_engine.find_serial(self.store, ' imei-222 ').status, 'IN_STOCK')
        self.assertEqual(serial_engine.find_serial(self.store, 'IMEI-111').status, 'SOLD')
        self.assertIsNone(serial_engine.find_serial(self.store, 'nope'))

    def test_pos_scan_of_serial_finds_product_and_preselects_it(self):
        r = self.till.get('/api/inventory/products/autocomplete/', {'q': 'imei-222', 'pos': '1'})
        self.assertEqual(r.status_code, 200, r.content)
        top = r.data['results'][0]
        self.assertEqual(top['id'], str(self.product.id))
        self.assertEqual(top['scan_serial'], 'IMEI-222')
        self.assertEqual(top['scan_serial_branch'], str(self.branch.id))
        self.assertTrue(top['track_serials'])
        # a SOLD serial is not offered for sale
        r = self.till.get('/api/inventory/products/autocomplete/', {'q': 'IMEI-111', 'pos': '1'})
        self.assertFalse(any(row.get('scan_serial') for row in r.data['results']))


class PreTrackingReturnTests(_SerialBase):
    """GATES already sells laptops: a unit sold BEFORE tracking was switched on has no serial row, and must
    still be returnable — with no serial, or with one typed now (review s168)."""

    def setUp(self):
        super().setUp()
        Product.objects.filter(pk=self.product.pk).update(track_serials=False)
        self.product.refresh_from_db()
        StockLevel.objects.create(variant=self.variant, branch=self.branch, quantity=Decimal('3'))
        self.old_inv = self._sell([], qty=2)                      # sold with no serials
        self.assertEqual(self._stock(), Decimal('1'))
        Product.objects.filter(pk=self.product.pk).update(track_serials=True)   # tracking switched on
        self.product.refresh_from_db()
        r = self.mgr.post('/api/inventory/serials/register/', {   # the unit still on the shelf gets its serial
            'variant': str(self.variant.id), 'branch': str(self.branch.id), 'serial_numbers': ['SHELF-1']},
            format='json')
        self.assertEqual(r.status_code, 201, r.content)

    def _refund(self, serials, qty=1, original=True, restock=True):
        body = {'branch': str(self.branch.id), 'customer': str(self.customer.id), 'refund_method': 'CASH',
                'items': [{'variant': str(self.variant.id), 'quantity': str(qty), 'refund_amount': '20000',
                           'restock_inventory': restock, 'serial_numbers': serials}]}
        if original:
            body['original_invoice'] = self.old_inv
        return self.mgr.post('/api/finance/refunds/', body, format='json')

    def test_old_sale_returns_without_serial_then_with_typed_serial(self):
        r = self._refund([])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self._stock(), Decimal('2'))
        ref = RefundInvoice.objects.get(pk=r.data['id'])
        self.assertEqual((ref.total_refunded, ref.paid_back), (Decimal('20000'), Decimal('20000')))
        self.assertEqual(SerialUnit.all_objects.filter(status='IN_STOCK').count(), 1)   # serials ≤ stock

        r = self._refund(['old-2'])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self._stock(), Decimal('3'))
        u = self._unit('OLD-2')
        self.assertEqual((u.status, u.branch_id), ('IN_STOCK', self.branch.id))
        self.assertTrue(SerialMove.all_objects.filter(unit=u, kind='RETURNED').exists())
        self._sell(['OLD-2'])                                       # sellable by serial from now on
        self.assertEqual(self._unit('OLD-2').status, 'SOLD')

        r = self._refund([])                                        # both old units are back already
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(self._stock(), Decimal('2'))

    def test_typed_serial_of_a_unit_sold_by_serial_is_refused(self):
        self._sell(['SHELF-1'])
        r = self._refund(['SHELF-1'])                               # sold by serial on ANOTHER sale
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_not_sold')
        self.assertEqual(self._unit('SHELF-1').status, 'SOLD')

    def test_no_receipt_return_of_pre_tracking_unit(self):
        r = self._refund([], original=False)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self._stock(), Decimal('1') + Decimal('1'))

    def test_sale_by_serial_still_needs_its_serial_back(self):
        new_inv = self._sell(['SHELF-1'])
        body = {'branch': str(self.branch.id), 'customer': str(self.customer.id), 'original_invoice': new_inv,
                'items': [{'variant': str(self.variant.id), 'quantity': '1', 'refund_amount': '1',
                           'restock_inventory': True, 'serial_numbers': []}]}
        r = self.mgr.post('/api/finance/refunds/', body, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_count')


    def test_no_receipt_serial_return_does_not_free_a_serialless_return(self):
        """s168 review: S sold by serial on #A → returned with NO receipt (by serial) → a second return against #A
        with no serial must be refused (it would refund the laptop twice and add a phantom unit)."""
        new_inv = self._sell(['SHELF-1'])
        body = {'branch': str(self.branch.id), 'customer': str(self.customer.id), 'refund_method': 'CASH',
                'items': [{'variant': str(self.variant.id), 'quantity': '1', 'refund_amount': '1',
                           'restock_inventory': True, 'serial_numbers': ['SHELF-1']}]}
        r = self.mgr.post('/api/finance/refunds/', body, format='json')          # no original_invoice
        self.assertEqual(r.status_code, 201, r.content)
        stock = self._stock()
        body['original_invoice'] = new_inv
        body['items'][0]['serial_numbers'] = []
        r = self.mgr.post('/api/finance/refunds/', body, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(self._stock(), stock)


class TillAddPathTests(_SerialBase):
    """Every way the till adds a product tells it the product is serial-tracked (→ the serial picker)."""

    def test_favourite_and_top_selling_carry_track_serials(self):
        from pos.models import POSFavoriteItem
        POSFavoriteItem.objects.create(store=self.store, product=self.product)
        r = self.till.get('/api/pos/favorites/')
        self.assertEqual(r.status_code, 200, r.content)
        favs = r.data.get('results', r.data) if isinstance(r.data, dict) else r.data
        self.assertTrue(favs[0]['track_serials'])
        self._stock_in(['F-1'])
        self._sell(['F-1'])
        r = self.till.get('/api/pos/top-selling/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.data[0]['track_serials'])

    def test_line_without_serials_never_posts(self):
        self._stock_in(['F-2'])
        r = self._checkout(self._cart([], qty=1))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data['serial_code'], 'serial_count')
        self.assertEqual(self._stock(), Decimal('1'))


class HelperTests(TestCase):

    def test_add_months_clamps_month_end(self):
        self.assertEqual(serial_engine.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(serial_engine.add_months(date(2026, 10, 14), 12), date(2027, 10, 14))
        self.assertEqual(serial_engine.add_months(date(2026, 11, 30), 3), date(2027, 2, 28))

    def test_normalize(self):
        self.assertEqual(serial_engine.normalize(' 35 209900 176148 1 '), '352099001761481')
        self.assertEqual(serial_engine.normalize('abC-1'), 'ABC-1')
