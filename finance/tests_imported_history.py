"""History imported from another system (PROLINE-IMPORT-B.md) meets today's money: a return from an IMPORTED sale and
the weighted-average cost. The sale / purchase rows are made exactly as data_import makes them (settled, no Payment)."""
from decimal import Decimal

from django.utils import timezone

from finance.models import (PurchaseInvoice, PurchaseItem, PurchaseReturn, PurchaseReturnItem, RefundInvoice,
                            SalesInvoice, SalesInvoiceItem, WorkShift, weighted_avg_cost)
from finance.tests_treasury_flows import _Base
from treasury import ledger

D = Decimal


def imported_sale(base, total='100', qty='1'):
    inv = SalesInvoice.all_objects.create(store=base.store, branch=base.branch, customer=base.customer,
                                          status=SalesInvoice.Status.POSTED, date=timezone.now(), invoice_number=5000,
                                          subtotal=D(total), grand_total=D(total), paid_amount=D(total),
                                          source_number='5000')
    SalesInvoiceItem.objects.bulk_create([SalesInvoiceItem(invoice=inv, variant=base.variant, quantity=D(qty),
                                                           unit_price=D(total) / D(qty), total=D(total))])
    return inv


class ReturnFromImportedSaleTreasuryOn(_Base):
    TREASURY = True
    CODE = '311'

    def test_the_payout_is_drawer_cash_from_the_refunders_shift(self):
        r = self.open_shift(self.manager, '0')
        self.assertEqual(r.status_code, 201, r.content)
        shift = WorkShift.all_objects.get(pk=r.data['id'])
        ledger.transfer(self.safe, self.drawer, '300')
        inv = imported_sale(self)
        r = self.refund(self.manager, inv, '100')
        self.assertEqual(r.status_code, 201, r.content)
        refund = RefundInvoice.all_objects.get(pk=r.data['id'])
        self.assertEqual((refund.drawer_cash, refund.paid_back, refund.shift_id), (D('100'), D('100'), shift.pk))
        self.assertEqual(self.bal(self.drawer), D('200'))                  # out of the drawer, not "not sorted yet"
        self.assertEqual(self.bal(ledger.unsorted_box(self.store)), D('0'))


class ReturnFromImportedSaleTreasuryOff(_Base):
    TREASURY = False
    CODE = '312'

    def test_the_shifts_expected_cash_drops_by_the_refund(self):
        r = self.open_shift(self.manager, '500')
        shift = WorkShift.all_objects.get(pk=r.data['id'])
        inv = imported_sale(self)
        r = self.refund(self.manager, inv, '100')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(D(r.data['drawer_cash']), D('100'))
        self.assertEqual(shift.cash_out(), D('100'))
        shift.close_shift('400')
        self.assertEqual((shift.expected_cash, shift.difference), (D('400'), D('0')))


class WeightedAvgCostIgnoresImportedHistory(_Base):
    TREASURY = False
    CODE = '313'

    def _purchase(self, cost, qty, imported):
        inv = PurchaseInvoice.all_objects.create(store=self.store, branch=self.branch, supplier=self.sup,
                                                 date=timezone.now(), status=PurchaseInvoice.Status.RECEIVED,
                                                 source_number='7' if imported else '')
        PurchaseItem.objects.bulk_create([PurchaseItem(invoice=inv, variant=self.variant, quantity=D(qty),
                                                       unit_cost=D(cost), total_cost=D(cost) * D(qty))])
        return inv

    def test_only_todays_purchases_count(self):
        self.variant.cost_price = D('80')
        self.variant.save(update_fields=['cost_price'])
        old = self._purchase('10', '50', imported=True)             # years-old cheap price from the old system
        self.assertEqual(weighted_avg_cost(self.variant, self.store), D('80'))   # only imported → cost_price
        self._purchase('90', '2', imported=False)
        self.assertEqual(weighted_avg_cost(self.variant, self.store), D('90'))
        ret = PurchaseReturn.all_objects.create(store=self.store, branch=self.branch, supplier=self.sup,
                                                return_number=1, total_amount=D('100'), source_number='7')
        PurchaseReturnItem.objects.create(purchase_return=ret, variant=self.variant, purchase_item=old.items.get(),
                                          quantity=D('10'), unit_cost=D('10'), total_cost=D('100'))
        self.assertEqual(weighted_avg_cost(self.variant, self.store), D('90'))   # an imported return doesn't count
        # s173 2nd review: a return made TODAY (no source number) against the IMPORTED purchase line doesn't count either
        today = PurchaseReturn.all_objects.create(store=self.store, branch=self.branch, supplier=self.sup,
                                                  return_number=2, total_amount=D('10'))
        PurchaseReturnItem.objects.create(purchase_return=today, variant=self.variant, purchase_item=old.items.get(),
                                          quantity=D('1'), unit_cost=D('10'), total_cost=D('10'))
        self.assertEqual(weighted_avg_cost(self.variant, self.store), D('90'))
