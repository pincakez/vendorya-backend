"""Import a checked laptop price list into ONE shop (s160, Yakot 2026-10-07 — GATES' real list).

    manage.py import_laptop_list --store "GATES Technology" --data /path/to/dir [--wipe] [--dry-run]

`<data>/laptops.json` = a list of
    {codes: ["12511", "12513"], name, cpu, gpu, ram, storage, display, category, price,
     images: ["img/12511/1.jpg", …], description_ar: "### **…**"}
(the research step writes it — specs checked on the makers' sites, Arabic showcase text in the
SHOWCASE-WRITING-STYLE.md voice). Every product goes through the REAL API views as the shop's
owner, so SKU making, attributes, SKU2 checks, the purchase receive (stock ledger) and the photo
conversion (webp) all run exactly as when a person clicks.

- codes[0] → SKU2 (the Proline code — TODO §PROLINE-WALLS); a 2nd unit's code → its barcode, so a
  scan of either finds it. One unit per code (a price list has no quantities).
- Stock comes in the ERP way (Yakot, s160): ONE purchase invoice from the shop's supplier with every
  new laptop on it, then RECEIVED. Cost = the row's `cost` (0 when unknown — fix later on the invoice).
- --wipe soft-deletes the shop's current products first (they stay in the trash).
- Categories are matched by name; a missing one is created under "Laptop". SELECT attribute
  options are added when a value is new.
- Re-running skips a product whose name already exists (live) in the shop.
"""
import json
import mimetypes
import os

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Branch, Store
from core.tenancy import clear_current_request, set_current_request, set_current_store
from inventory.models import AttributeDefinition, Category, Product, ProductVariant, Supplier
from finance.views import PurchaseInvoiceViewSet
from inventory.views import ProductViewSet

ATTR = {'cpu': 'Cpu', 'gpu': 'Gpu', 'ram': 'Ram', 'storage': 'Storage', 'display': 'Display'}


class Command(BaseCommand):
    help = 'Import a checked laptop price list (laptops.json + img/) into one shop.'

    def add_arguments(self, parser):
        parser.add_argument('--store', required=True)
        parser.add_argument('--data', required=True)
        parser.add_argument('--wipe', action='store_true', help="soft-delete the shop's current products first")
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--reference', default='GATES price list 2026-10-07')

    def handle(self, *args, **o):
        store = Store.objects.filter(name=o['store'], is_deleted=False).first()
        if not store:
            raise CommandError(f"No shop named {o['store']!r}.")
        with open(os.path.join(o['data'], 'laptops.json'), encoding='utf-8') as f:
            rows = json.load(f)
        owner = store.owner
        branch = Branch.objects.filter(store=store, is_main_branch=True).first() or Branch.objects.filter(store=store).first()
        supplier = store.default_supplier or Supplier.objects.filter(store=store).order_by('created_at').first()
        if not (owner and branch and supplier):
            raise CommandError('The shop needs an owner, a branch and a supplier first.')
        self.stdout.write(f'{store.name}: {len(rows)} laptops · supplier {supplier.name} ({supplier.code_prefix}) · branch {branch.name}')
        if o['dry_run']:
            for r in rows:
                self.stdout.write(f"  {r['codes']} {r['name']} → {r['category']} · {r['price']} · {len(r.get('images', []))} photos")
            return

        if o['wipe']:
            now = timezone.now()
            n = Product.objects.filter(store=store).update(is_deleted=True, deleted_at=now)
            ProductVariant.objects.filter(store=store).update(is_deleted=True, deleted_at=now)
            self.stdout.write(f'  wiped {n} products (soft-deleted, in the trash)')

        laptop_root = Category.objects.filter(store=store, name='Laptop', parent__isnull=True).first()
        attrs = {a.name: a for a in AttributeDefinition.objects.filter(store=store)}
        factory = APIRequestFactory()

        def call(view, method, path, data=None, fmt='json', **kw):
            req = getattr(factory, method)(path, data, format=fmt, HTTP_HOST='127.0.0.1')
            force_authenticate(req, user=owner)
            set_current_request(req)
            set_current_store(store)
            try:
                resp = view(req, **kw)
                resp.render() if hasattr(resp, 'render') else None
                return resp
            finally:
                clear_current_request()

        create = ProductViewSet.as_view({'post': 'create'})
        upload = ProductViewSet.as_view({'post': 'upload_media'})
        show = ProductViewSet.as_view({'post': 'showcase_settings'})
        purchase_create = PurchaseInvoiceViewSet.as_view({'post': 'create'})
        purchase_receive = PurchaseInvoiceViewSet.as_view({'post': 'receive'})
        lines = []

        for r in rows:
            existing = Product.objects.filter(store=store, name=r['name']).first()
            if existing:
                from finance.models import PurchaseItem
                v = ProductVariant.objects.filter(product=existing).first()
                if v and not PurchaseItem.objects.filter(variant=v).exists():
                    lines.append({'variant': str(v.id), 'quantity': len(r['codes']),
                                  'base_price': r.get('cost', 0) or 0, 'retail_price': r['price']})
                    self.stdout.write(f"  exists, not purchased yet → on the invoice: {r['name']}")
                else:
                    self.stdout.write(f"  skip (exists): {r['name']}")
                continue
            cat = Category.objects.filter(store=store, name=r['category']).first()
            if cat is None:
                cat = Category.objects.create(store=store, name=r['category'], parent=laptop_root)
                self.stdout.write(f'  + category {cat.name}')
            attr_payload = []
            for key, name in ATTR.items():
                a, val = attrs.get(name), (r.get(key) or '').strip()
                if not (a and val):
                    continue
                if a.input_type == 'SELECT' and val not in (a.options or []):
                    a.options = list(a.options or []) + [val]
                    a.save(update_fields=['options', 'updated_at'])
                attr_payload.append({'definition': str(a.id), 'value': val})

            with transaction.atomic():
                resp = call(create, 'post', '/api/inventory/products/', {
                    'name': r['name'], 'description': r.get('description_ar', ''),
                    'category': str(cat.id), 'supplier': str(supplier.id),
                    'sell_price': r['price'], 'cost_price': 0, 'base_price': 0, 'reorder_level': 0,
                    'attributes': attr_payload, 'sku2': r['codes'][0],
                })
                if resp.status_code != 201:
                    raise CommandError(f"{r['name']}: create failed {resp.status_code} {resp.data}")
                product = Product.objects.get(pk=resp.data['id'])
                variant = ProductVariant.objects.filter(product=product).first()
                if len(r['codes']) > 1:
                    variant.barcode = r['codes'][1]
                    variant.save(update_fields=['barcode', 'updated_at'])
                lines.append({'variant': str(variant.id), 'quantity': len(r['codes']),
                              'base_price': r.get('cost', 0) or 0, 'retail_price': r['price']})

            photos = 0
            for rel in r.get('images', [])[:5]:
                path = os.path.join(o['data'], rel)
                with open(path, 'rb') as fh:
                    up = SimpleUploadedFile(os.path.basename(path), fh.read(),
                                            content_type=mimetypes.guess_type(path)[0] or 'image/jpeg')
                resp = call(upload, 'post', f'/api/inventory/products/{product.id}/upload-media/',
                            {'file': up, 'kind': 'image'}, fmt='multipart', pk=str(product.id))
                if resp.status_code == 201:
                    photos += 1
                else:
                    self.stderr.write(f'    photo {rel} refused: {resp.status_code} {resp.data}')
            call(show, 'post', f'/api/inventory/products/{product.id}/showcase-settings/',
                 {'showcase_lead': 'slideshow', 'showcase_autoplay': True}, pk=str(product.id))
            variant.refresh_from_db()
            self.stdout.write(f"  ✓ {r['name']} · SKU {variant.sku} · SKU2 {variant.sku2}"
                              f"{' · barcode ' + variant.barcode if variant.barcode else ''} · {photos} photos")

        if not lines:
            self.stdout.write('  nothing new — no purchase invoice made')
            return
        resp = call(purchase_create, 'post', '/api/finance/purchases/', {
            'supplier': str(supplier.id), 'branch': str(branch.id), 'date': timezone.now().isoformat(),
            'vendor_reference': o['reference'], 'notes': 'Imported from the GATES laptop price list (s160). '
            'Costs are placeholders until the real supplier prices arrive.', 'lines': lines,
        })
        if resp.status_code != 201:
            raise CommandError(f'purchase invoice failed {resp.status_code} {resp.data}')
        pid = resp.data['id']
        resp = call(purchase_receive, 'post', f'/api/finance/purchases/{pid}/receive/', {}, pk=pid)
        if resp.status_code != 200:
            raise CommandError(f'receive failed {resp.status_code} {resp.data}')
        self.stdout.write(f"  ✓ purchase #{resp.data.get('purchase_number')} from {supplier.name} RECEIVED — "
                          f"{len(lines)} lines, {sum(l['quantity'] for l in lines)} units")
