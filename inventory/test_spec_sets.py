"""Category spec sets (PROLINE-PARITY Upgrade 9, s168):
- a category carries an ordered list of the shop's attributes; a sub-category inherits its parents';
- one-click presets create missing attributes and never duplicate;
- another shop's attribute / category is refused;
- the product form's required specs + NUMBER values are checked on save;
- label stickers carry the specs on one line.
"""
from decimal import Decimal

from django.conf import settings as dj_settings
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Store, StoreSettings
from inventory.models import (AttributeDefinition, Category, CategorySpec, Product, ProductAttribute,
                              ProductVariant, Supplier)
from inventory.spec_presets import SPEC_PRESETS
from inventory.spec_sets import effective_spec_set
from users.models import User

CATS = '/api/inventory/categories/'


def make_shop(code, lang='en'):
    owner = User.objects.create_user(username=f'spec_own{code}', password='x', role='OWNER')
    store = Store.objects.create(name=f'Spec {code}', store_code=code, owner=owner, default_language=lang)
    owner.store = store
    owner.save(update_fields=['store'])
    StoreSettings.objects.get_or_create(store=store)
    return store, owner


def client_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


class SpecSetTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.store, self.owner = make_shop('320')
        self.api = client_for(self.owner)
        mk = lambda key, name, **kw: AttributeDefinition.objects.create(store=self.store, key=key, name=name, **kw)
        self.cpu = mk('cpu', 'CPU')
        self.ram = mk('ram', 'RAM', input_type='SELECT', options=['8GB', '16GB'])
        self.gpu = mk('gpu', 'GPU')
        self.screen = mk('display', 'Screen', input_type='NUMBER', unit='"')
        self.computers = Category.objects.create(store=self.store, name='Computers')
        self.laptops = Category.objects.create(store=self.store, name='Laptops', parent=self.computers)
        self.gaming = Category.objects.create(store=self.store, name='Gaming', parent=self.laptops)

    def _put(self, cat, items, api=None):
        return (api or self.api).put(f'{CATS}{cat.id}/specs/', {'items': items}, format='json')

    # ── inheritance ──
    def test_child_inherits_parent_spec_set_then_adds_its_own(self):
        self.assertEqual(self._put(self.computers, [{'definition': str(self.cpu.id)}]).status_code, 200)
        self._put(self.laptops, [{'definition': str(self.ram.id)}, {'definition': str(self.screen.id)}])
        r = self._put(self.gaming, [{'definition': str(self.gpu.id)},
                                    {'definition': str(self.ram.id), 'required': True}])
        self.assertEqual(r.status_code, 200, r.content)
        data = self.api.get(f'{CATS}{self.gaming.id}/specs/').json()
        eff = data['effective']
        # root first, each level in its own order; RAM listed once, at its first (Laptops) position
        self.assertEqual([s['key'] for s in eff], ['cpu', 'ram', 'display', 'gpu'])
        ram = eff[1]
        self.assertTrue(ram['required'])                       # Gaming makes it required
        self.assertEqual(ram['inherited_from']['name'], 'Laptops')
        self.assertIsNone(eff[3]['inherited_from'])            # GPU is Gaming's own
        self.assertEqual([s['key'] for s in data['own']], ['gpu'])
        self.assertEqual(eff[2]['unit'], '"')
        # the parent does NOT see the child's lines
        self.assertEqual([s['key'] for s in effective_spec_set(self.laptops)], ['cpu', 'ram', 'display'])

    def test_deleted_attribute_drops_out_of_the_set(self):
        self._put(self.laptops, [{'definition': str(self.cpu.id)}, {'definition': str(self.gpu.id)}])
        self.gpu.delete()   # soft
        self.assertEqual([s['key'] for s in effective_spec_set(self.laptops)], ['cpu'])

    def test_reorder_remove_and_readd_is_soft_and_reuses_the_row(self):
        self._put(self.laptops, [{'definition': str(self.cpu.id)}, {'definition': str(self.ram.id)}])
        row = CategorySpec.objects.get(category=self.laptops, definition=self.ram)
        self._put(self.laptops, [{'definition': str(self.cpu.id)}])
        row.refresh_from_db()
        self.assertTrue(row.is_deleted)                        # soft delete, never a DELETE
        r = self._put(self.laptops, [{'definition': str(self.ram.id)}, {'definition': str(self.cpu.id)}])
        self.assertEqual([s['key'] for s in r.json()['own']], ['ram', 'cpu'])
        self.assertEqual(CategorySpec.all_objects.filter(category=self.laptops).count(), 2)
        row.refresh_from_db()
        self.assertFalse(row.is_deleted)

    def test_same_attribute_twice_is_refused(self):
        r = self._put(self.laptops, [{'definition': str(self.cpu.id)}, {'definition': str(self.cpu.id)}])
        self.assertEqual(r.status_code, 400)

    # ── tenant isolation ──
    def test_other_shops_attribute_is_refused(self):
        other, _ = make_shop('321')
        foreign = AttributeDefinition.objects.create(store=other, key='cpu', name='CPU')
        r = self._put(self.laptops, [{'definition': str(self.cpu.id)}, {'definition': str(foreign.id)}])
        self.assertEqual(r.status_code, 400)
        self.assertFalse(CategorySpec.all_objects.filter(category=self.laptops).exists())   # nothing half-written
        r = self._put(self.laptops, [{'definition': 'not-a-uuid'}])
        self.assertEqual(r.status_code, 400)

    def test_other_shops_category_is_invisible(self):
        other, other_owner = make_shop('322')
        api2 = client_for(other_owner)
        self.assertEqual(api2.get(f'{CATS}{self.laptops.id}/specs/').status_code, 404)
        self.assertEqual(self._put(self.laptops, [], api=api2).status_code, 404)
        self.assertEqual(api2.post(f'{CATS}{self.laptops.id}/apply-preset/', {'preset': 'laptop'},
                                   format='json').status_code, 404)

    def test_roles(self):
        cashier = User.objects.create_user(username='spec_csh', password='x', role='CASHIER', store=self.store)
        manager = User.objects.create_user(username='spec_mgr', password='x', role='MANAGER', store=self.store)
        c, m = client_for(cashier), client_for(manager)
        self.assertEqual(c.get(f'{CATS}{self.laptops.id}/specs/').status_code, 200)     # the product form reads it
        self.assertEqual(self._put(self.laptops, [], api=c).status_code, 403)
        self.assertEqual(self._put(self.laptops, [{'definition': str(self.cpu.id)}], api=m).status_code, 200)
        self.assertEqual(c.get(f'{CATS}spec-presets/').status_code, 403)
        self.assertEqual(m.get(f'{CATS}spec-presets/').status_code, 200)
        # a preset can create attributes → same gate as creating an attribute (ADMIN)
        self.assertEqual(m.post(f'{CATS}{self.laptops.id}/apply-preset/', {'preset': 'laptop'},
                                format='json').status_code, 403)

    def test_spec_count_on_the_category_list(self):
        self._put(self.laptops, [{'definition': str(self.cpu.id)}, {'definition': str(self.gpu.id)}])
        rows = {c['name']: c for c in self.api.get(CATS).json()}
        self.assertEqual(rows['Laptops']['spec_count'], 2)
        self.assertEqual(rows['Gaming']['spec_count'], 0)

    # ── presets ──
    def test_preset_applied_twice_creates_nothing_twice(self):
        url = f'{CATS}{self.laptops.id}/apply-preset/'
        r1 = self.api.post(url, {'preset': 'laptop'}, format='json')
        self.assertEqual(r1.status_code, 200, r1.content)
        n_fields = len(SPEC_PRESETS['laptop']['fields'])
        defs_after_first = AttributeDefinition.objects.filter(store=self.store).count()
        self.assertEqual(len(r1.json()['effective']), n_fields)
        # cpu / ram / gpu / display already existed → reused, untouched
        self.assertNotIn('RAM', r1.json()['created'])
        self.ram.refresh_from_db()
        self.assertEqual(self.ram.options, ['8GB', '16GB'])
        self.assertEqual(AttributeDefinition.objects.filter(store=self.store, key='ram').count(), 1)

        r2 = self.api.post(url, {'preset': 'laptop'}, format='json')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()['created'], [])
        self.assertEqual(r2.json()['attached'], [])
        self.assertEqual(AttributeDefinition.objects.filter(store=self.store).count(), defs_after_first)
        self.assertEqual(CategorySpec.objects.filter(category=self.laptops).count(), n_fields)

    def test_preset_skips_what_a_parent_already_has_and_uses_the_shop_language(self):
        ar_store, ar_owner = make_shop('323', lang='ar')
        root = Category.objects.create(store=ar_store, name='أجهزة')
        child = Category.objects.create(store=ar_store, name='موبايلات', parent=root)
        api = client_for(ar_owner)
        api.post(f'{CATS}{root.id}/apply-preset/', {'preset': 'monitor'}, format='json')
        r = api.post(f'{CATS}{child.id}/apply-preset/', {'preset': 'phone'}, format='json')
        self.assertEqual(r.status_code, 200)
        own_keys = [s['key'] for s in r.json()['own']]
        self.assertNotIn('brand', own_keys)          # inherited from the parent's monitor preset
        self.assertNotIn('display', own_keys)
        self.assertIn('storage', own_keys)
        self.assertEqual(AttributeDefinition.objects.get(store=ar_store, key='storage').name, 'التخزين')
        self.assertEqual(AttributeDefinition.objects.filter(store=ar_store, key='brand').count(), 1)

    def test_unknown_preset(self):
        r = self.api.post(f'{CATS}{self.laptops.id}/apply-preset/', {'preset': 'spaceship'}, format='json')
        self.assertEqual(r.status_code, 400)


class ProductSpecValueTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.store, self.owner = make_shop('330')
        self.api = client_for(self.owner)
        self.sup = Supplier.objects.create(store=self.store, name='Sup', code_prefix='400', prefix_locked=True)
        self.cpu = AttributeDefinition.objects.create(store=self.store, key='cpu', name='CPU')
        self.ram = AttributeDefinition.objects.create(store=self.store, key='ram', name='RAM', unit='GB',
                                                      input_type='NUMBER')
        self.screen = AttributeDefinition.objects.create(store=self.store, key='display', name='Screen',
                                                         input_type='NUMBER', unit='"')
        self.color = AttributeDefinition.objects.create(store=self.store, key='color', name='Color')
        self.laptops = Category.objects.create(store=self.store, name='Laptops')
        self.gaming = Category.objects.create(store=self.store, name='Gaming', parent=self.laptops)
        CategorySpec.objects.create(category=self.laptops, definition=self.cpu, sort_order=0, required=True)
        CategorySpec.objects.create(category=self.laptops, definition=self.ram, sort_order=1)
        CategorySpec.objects.create(category=self.laptops, definition=self.screen, sort_order=2)

    def _create(self, category, attrs):
        return self.api.post('/api/inventory/products/', {
            'name': 'ThinkPad', 'supplier': str(self.sup.id), 'category': str(category.id),
            'sell_price': '25000', 'attributes': attrs}, format='json')

    def _a(self, d, v):
        return {'definition': str(d.id), 'value': v}

    def test_required_inherited_spec_must_be_filled(self):
        r = self._create(self.gaming, [self._a(self.ram, '16')])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['missing_specs'], ['CPU'])

    def test_product_saves_its_spec_values(self):
        r = self._create(self.gaming, [self._a(self.cpu, 'i7-1185G7'), self._a(self.ram, '16'),
                                       self._a(self.color, 'Black')])
        self.assertEqual(r.status_code, 201, r.content)
        v = Product.objects.get(id=r.json()['id']).variants.first()
        vals = {a.definition.key: a.value for a in v.attributes.select_related('definition')}
        self.assertEqual(vals, {'cpu': 'i7-1185G7', 'ram': '16', 'color': 'Black'})

    def test_number_spec_takes_numbers_only(self):
        r = self._create(self.laptops, [self._a(self.cpu, 'i5'), self._a(self.ram, 'sixteen')])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['not_a_number'], ['RAM'])

    def test_edit_keeps_unsent_required_value_but_refuses_clearing_it(self):
        r = self._create(self.laptops, [self._a(self.cpu, 'i5')])
        pid = r.json()['id']
        url = f'/api/inventory/products/{pid}/'
        ok = self.api.patch(url, {'name': 'ThinkPad X1', 'attributes': [self._a(self.ram, '8')]}, format='json')
        self.assertEqual(ok.status_code, 200, ok.content)            # CPU not sent → stored value counts
        bad = self.api.patch(url, {'attributes': [self._a(self.cpu, '')]}, format='json')
        self.assertEqual(bad.status_code, 400)
        # a PATCH without attributes (bulk edit, ghosting…) is never blocked by the spec set
        self.assertEqual(self.api.patch(url, {'name': 'X1'}, format='json').status_code, 200)

    def test_product_made_before_the_spec_set_can_still_be_bulk_edited(self):
        p = Product.objects.create(store=self.store, name='Old', supplier=self.sup, category=self.laptops)
        ProductVariant.objects.create(product=p, sku='901', sell_price=Decimal('1'))
        r = self.api.patch(f'/api/inventory/products/{p.id}/', {'description': 'x'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)


class SpecLabelTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.store, self.owner = make_shop('340')
        self.api = client_for(self.owner)
        sup = Supplier.objects.create(store=self.store, name='Sup', code_prefix='400', prefix_locked=True)
        mk = lambda key, name, **kw: AttributeDefinition.objects.create(store=self.store, key=key, name=name, **kw)
        self.cpu, self.ram = mk('cpu', 'CPU'), mk('ram', 'RAM', unit='GB')
        self.storage, self.screen = mk('storage', 'Storage'), mk('display', 'Screen', unit='"')
        self.color = mk('color', 'Color')
        parent = Category.objects.create(store=self.store, name='Computers')
        laptops = Category.objects.create(store=self.store, name='Laptops', parent=parent)
        CategorySpec.objects.create(category=parent, definition=self.cpu, sort_order=0)
        for i, d in enumerate([self.ram, self.storage, self.screen]):
            CategorySpec.objects.create(category=laptops, definition=d, sort_order=i)
        self.p = Product.objects.create(store=self.store, name='ThinkPad', supplier=sup, category=laptops)
        v = ProductVariant.objects.create(product=self.p, sku='777', sell_price=Decimal('25000'))
        for d, val in [(self.screen, '14'), (self.ram, '16GB'), (self.cpu, 'i7-1185G7'),
                       (self.storage, '512GB SSD'), (self.color, 'Black')]:
            ProductAttribute.objects.create(variant=v, definition=d, value=val)
        self.plain = Product.objects.create(store=self.store, name='Cable', supplier=sup)
        ProductVariant.objects.create(product=self.plain, sku='778', sell_price=Decimal('50'))

    def test_label_data_carries_the_spec_line_in_spec_set_order(self):
        r = self.api.post('/api/inventory/products/label-data/',
                          {'ids': [str(self.p.id), str(self.plain.id)]}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        items = {i['product_name']: i for i in r.json()['items']}
        # root's CPU first, then Laptops' order; a value already ending in its unit isn't doubled;
        # Color is an attribute but not in the spec set → not on the sticker
        self.assertEqual(items['ThinkPad']['specs'], 'i7-1185G7 · 16GB · 512GB SSD · 14"')
        self.assertEqual(items['Cable']['specs'], '')

    def test_purchase_label_data_carries_the_same_spec_line(self):
        from django.utils import timezone
        from core.models import Address, Branch
        from finance.models import PurchaseInvoice, PurchaseItem
        branch = Branch.objects.create(store=self.store, name='Main',
                                       address=Address.objects.create(store=self.store, street_1='1', city='Cairo'))
        inv = PurchaseInvoice.objects.create(store=self.store, branch=branch, supplier=self.p.supplier,
                                             date=timezone.now())
        PurchaseItem.objects.create(invoice=inv, variant=self.p.variants.first(), quantity=Decimal('2'),
                                    unit_cost=Decimal('20000'), total_cost=Decimal('40000'))
        r = self.api.get(f'/api/finance/purchases/{inv.id}/label-data/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['items'][0]['specs'], 'i7-1185G7 · 16GB · 512GB SSD · 14"')

    def test_label_preset_has_an_opt_in_specs_switch(self):
        r = self.api.post('/api/core/label-presets/', {'name': 'Laptop tag', 'width_mm': 50, 'height_mm': 30},
                          format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertFalse(r.json()['show_specs'])                      # off unless the shop ticks it
        r = self.api.patch(f"/api/core/label-presets/{r.json()['id']}/", {'show_specs': True}, format='json')
        self.assertTrue(r.json()['show_specs'])
