"""Self-check for build_selling_units tier/sellable logic (§MU-MB).

DB-free: stubs the product/variant and patches the two store gate functions.
Run (inventory AppConfig imports models at load, so go through the shell):
  venv/bin/python manage.py shell -c "exec(open('inventory/test_selling_units.py').read())"
"""
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vendorya_project.settings')
if not django.apps.apps.ready:
    django.setup()

from inventory import serializers as S


class FakeQS(list):
    def filter(self, **kw):  # ignore is_deleted filter — stubs are all live
        return self
    def order_by(self, *a):
        return self


class FakeUnit:
    def __init__(self, name, factor, sellable):
        self.id, self.name, self.factor = name, name, factor
        self.sell_price, self.barcode, self.sellable = '1', None, sellable


class FakeVariant:
    def __init__(self, units):
        self.sell_price, self.barcode = '1', None
        self.selling_units = FakeQS(units)


class FakeProduct:
    selling_mode = 'UNIT'
    store_id = 1
    unit = 'Tablet'
    base_price = '1'
    def __init__(self, sell_base_unit):
        self.sell_base_unit = sell_base_unit


def run(sell_base_unit, strip_sellable):
    variant = FakeVariant([FakeUnit('Strip', '10', strip_sellable),
                           FakeUnit('Pack', '30', True)])
    units = S.build_selling_units(variant, FakeProduct(sell_base_unit))
    return [u['name'] for u in units if u['sellable']]


# force the multi-unit gate on, weight gate off
S.is_multi_unit_enabled = lambda sid: True
S.is_weight_selling_enabled = lambda sid: False

# 3 tiers: base(Tablet) + Strip + Pack all sellable
assert run(True, True) == ['Tablet', 'Strip', 'Pack'], run(True, True)
# Strip off ⇒ Pack + Unit only, Strip present but not sellable
assert run(True, False) == ['Tablet', 'Pack'], run(True, False)
# Strip + Unit off ⇒ single sellable unit (Pack) ⇒ POS skips the picker
assert run(False, False) == ['Pack'], run(False, False)
# Unit off but Strip on ⇒ Strip + Pack
assert run(False, True) == ['Strip', 'Pack'], run(False, True)

print("build_selling_units tier/sellable check: PASS")
