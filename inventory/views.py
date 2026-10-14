import re
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.utils import timezone
from django.db.models import (Q, Sum, F, Min, Value, OuterRef, Subquery, ExpressionWrapper, DecimalField,
                              Case, When, IntegerField)
from django.db.models.functions import Coalesce
from rest_framework import viewsets, filters, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from users.permissions import RoleScopedPermission, IsManagerOrAbove, IsAdminOrAbove
from .models import (
    Product, Category, Supplier, AttributeDefinition, ProductVariant, Tax,
    ProductAttribute, StockAdjustment, StockTransfer, ProductMedia,
    StorageLocation, StorageStock, StorageMovement,
)
from .serializers import (
    ProductListSerializer, ProductDetailSerializer, ProductWriteSerializer,
    ProductVariantSerializer, ProductMediaSerializer,
    CategorySerializer, SupplierSerializer, AttributeDefinitionSerializer, TaxSerializer,
    StockAdjustmentSerializer, StockTransferSerializer,
    StorageLocationSerializer, StorageStockSerializer, StorageMovementSerializer,
)
from . import media_processing
from rest_framework.parsers import MultiPartParser as _MultiPartParser, FormParser as _FormParser
from . import storage_service
from core.activity import log_activity
from core.models import ActivityLog
from core.field_visibility import hidden_fields_for

# Maps a sort key -> the output field it would reveal the order of.
_ORDER_TO_FIELD = {'o_wholesale': 'cost_display', 'o_retail': 'price_display', 'o_profit': 'profit_display'}


class VisibilityOrderingFilter(filters.OrderingFilter):
    """Drop ordering by fields the user isn't permitted to see (prevents
    inferring a hidden column's order). Pairs with FieldVisibilityMixin."""
    def remove_invalid_fields(self, queryset, fields, view, request):
        valid = super().remove_invalid_fields(queryset, fields, view, request)
        hidden = hidden_fields_for(request.user, getattr(view, 'fv_table_id', None))
        if not hidden:
            return valid
        blocked = {k for k, f in _ORDER_TO_FIELD.items() if f in hidden}
        return [t for t in valid if t.lstrip('-') not in blocked]

    def filter_queryset(self, request, queryset, view):
        qs = super().filter_queryset(request, queryset, view)
        # A search that EXACTLY matches a code (ProductSearchFilter's `_code_exact`) stays on top,
        # whatever column the list is sorted by — the sort applies below it.
        if '_code_exact' in qs.query.annotations:
            current = list(qs.query.order_by) or list(qs.model._meta.ordering)
            qs = qs.order_by('_code_exact', *current)
        return qs


class AttributeDefinitionViewSet(viewsets.ModelViewSet):
    serializer_class = AttributeDefinitionSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'ADMIN',
        'update':         'ADMIN',
        'partial_update': 'ADMIN',
        'destroy':        'ADMIN',
        'add_option':     'MANAGER',   # add a dropdown value from the product form (was owner-only by omission — s156)
    }

    def get_queryset(self):
        return AttributeDefinition.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)

    @action(detail=True, methods=['post'], url_path='add-option')
    def add_option(self, request, pk=None):
        """Append a new option to a SELECT attribute's option list."""
        attr = self.get_object()
        if attr.input_type != AttributeDefinition.InputType.SELECT:
            return Response({'detail': 'Only SELECT attributes support options.'}, status=400)
        value = (request.data.get('value') or '').strip()
        if not value:
            return Response({'detail': 'value is required.'}, status=400)
        current = list(attr.options or [])
        if value in current:
            return Response({'detail': 'Option already exists.'}, status=400)
        current.append(value)
        attr.options = current
        attr.save(update_fields=['options'])
        return Response({'options': attr.options})


class MemoryBasePagination(PageNumberPagination):
    """Pagination used ONLY for the Memory Base page (?source=memory_base).
    The reference pool holds tens of thousands of rows, so it must page; the
    sellable STORE inventory and the autofill (?source=all) stay unpaginated
    so POS / Purchases keep receiving a bare array as they expect."""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class ProductSearchFilter(filters.SearchFilter):
    """Name search as before, OR any code (SKU / SKU2 / barcode / pack barcode) containing the
    text — dashes ignored, SKU2 only while the shop's SKU2 is ON (s159). An EXACT code match is
    flagged `_code_exact=0` so VisibilityOrderingFilter can list it first (like the POS scan)."""

    def filter_queryset(self, request, queryset, view):
        terms = self.get_search_terms(request)
        if not terms:
            return queryset
        from inventory.code_search import code_contains, exact_code
        from inventory.sku import sku2_active
        by_name = super().filter_queryset(request, queryset, view)
        text = ' '.join(terms)
        store = getattr(request.user, 'store', None)
        with_sku2 = bool(store) and sku2_active(store)
        return queryset.filter(Q(pk__in=by_name.values('pk')) |
                               Q(code_contains(text, with_sku2))).annotate(
            _code_exact=Case(When(exact_code(text, with_sku2), then=Value(0)),
                             default=Value(1), output_field=IntegerField()))


class _AtomicWritesMixin:
    """create / update run in ONE transaction, so the per-shop barcode lock taken while validating
    (`lock_shop_barcodes`) is held until the row is saved and committed (s166)."""

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @transaction.atomic
    def update(self, request, *args, **kwargs):   # partial_update goes through here too
        return super().update(request, *args, **kwargs)


class ProductViewSet(_AtomicWritesMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
        # bulk + ghost
        'toggle_ghost':   'MANAGER',
        'bulk_ghost':     'MANAGER',
        'bulk_update':    'MANAGER',
        'bulk_delete':    'ADMIN',
        'upload_image':   'MANAGER',
        'remove_image':   'MANAGER',
        'upload_media':   'MANAGER',
        'delete_media':   'MANAGER',
        'reorder_media':  'MANAGER',
        'showcase_settings': 'MANAGER',
        'import_memory_base': 'MANAGER',
        'dedup_memory_base': 'MANAGER',
        'autocomplete': 'CASHIER',
        'alternatives': 'CASHIER',   # POS /sameing + /sametrade finder (was owner-only by omission — s156)
        'media_specs':  'MANAGER',   # upload-limits note on the product page (s156),
        # s167: price stickers from the product list. Cashiers print labels (s157 §AUDIT B7 opened the
        # presets to them); a sticker only shows what the till already shows (name, codes, sell price).
        'label_data':   'CASHIER',
    }
    filter_backends = [ProductSearchFilter, VisibilityOrderingFilter]
    fv_table_id = 'inventory_products'
    # Keep search_fields lean — no category joins (those caused 3-4s lag over 27k MB rows).
    # Attribute search is handled by the dedicated /autocomplete/ action.
    # SKU / SKU2 / barcode are matched by ProductSearchFilter itself (dashes ignored, s159).
    search_fields = ['name']
    # Server-side sort. FE maps column keys -> these (see Products.vue ORDER_MAP).
    ordering_fields = ['name', 'supplier__name', 'created_at',
                       'o_sku', 'o_wholesale', 'o_retail', 'o_profit', 'o_stock']

    # Reserved params that are NOT dynamic attribute filters.
    _RESERVED_PARAMS = {'search', 'ordering', 'page', 'page_size', 'category', 'source'}

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return ProductWriteSerializer
        if self.action == 'retrieve':
            return ProductDetailSerializer
        return ProductListSerializer

    @property
    def paginator(self):
        """Paginate ONLY the Memory Base list (?source=memory_base). Every other
        product request (POS, Purchases autofill, the items list) stays unpaginated
        and returns a bare array, which those consumers read directly."""
        if not hasattr(self, '_paginator'):
            source = (self.request.query_params.get('source') or '').lower()
            self._paginator = MemoryBasePagination() if source == 'memory_base' else None
        return self._paginator

    def get_queryset(self):
        qs = Product.objects.filter(store=self.request.user.store).select_related(
            # Walk the category parent chain (max 4 tiers) so per-level category
            # columns don't trigger a query per row.
            'category', 'category__parent',
            'category__parent__parent', 'category__parent__parent__parent',
        ).prefetch_related(
            'supplier',
            'variants', 'variants__stock_levels',
            'variants__attributes', 'variants__attributes__definition',
            'media',
        )

        # Memory Base isolation. MEMORY_BASE products are a supplier-less, SKU-less
        # reference pool that feeds autofill — they must NEVER appear in POS or the
        # inventory items list (the 27k seeded meds would flood both). So the default
        # (no param) returns only real, sellable STORE inventory. The dedicated Memory
        # Base page asks for ?source=memory_base; the catalog autofill asks for
        # ?source=all to search both pools.
        source = (self.request.query_params.get('source') or '').lower()
        if source == 'memory_base':
            qs = qs.filter(source=Product.Source.MEMORY_BASE)
        elif source == 'all':
            pass  # both pools — used by the New Purchase / New Invoice catalog autofill
        else:
            qs = qs.filter(source=Product.Source.STORE)

        # Sort annotations. Min over variants is fan-out-safe (idempotent); stock
        # uses a subquery so the Sum isn't multiplied by the variant join.
        stock_sq = Subquery(
            ProductVariant.objects.filter(product=OuterRef('pk'))
            .values('product')
            .annotate(t=Coalesce(Sum('stock_levels__quantity'), Value(Decimal('0'))))
            .values('t')[:1]
        )
        qs = qs.annotate(
            o_sku=Min('variants__sku'),
            o_wholesale=Min('variants__cost_price'),
            o_retail=Min('variants__sell_price'),
            o_stock=Coalesce(stock_sq, Value(Decimal('0')), output_field=DecimalField()),
        ).annotate(
            o_profit=ExpressionWrapper(F('o_retail') - F('o_wholesale'), output_field=DecimalField()),
        )

        params = self.request.query_params

        # Category quick-filter
        category = params.get('category')
        if category:
            qs = qs.filter(category_id=category)

        # Dynamic attribute filters: ?season=AW25&gender=Men
        for key, value in params.items():
            if key not in self._RESERVED_PARAMS:
                qs = qs.filter(variants__attributes__definition__key=key,
                               variants__attributes__value=value).distinct()
        return qs

    def perform_create(self, serializer):
        from billing.quota import enforce_quota
        enforce_quota(self.request.user.store, 'products')
        serializer.save(store=self.request.user.store)

    # ---- Ghost (hide from POS) ----

    def _store_products(self, ids):
        """Active products from THIS store matching the given ids. Store-scoped
        so one tenant can never touch another's rows even by guessing ids."""
        if not isinstance(ids, list) or not ids:
            return None
        return Product.objects.filter(store=self.request.user.store, id__in=ids)

    @action(detail=True, methods=['post'], url_path='upload-image', parser_classes=[_MultiPartParser, _FormParser])
    def upload_image(self, request, pk=None):
        """Upload or replace the product image. Send as multipart/form-data with key 'image'."""
        product = self.get_object()
        img = request.FILES.get('image')
        if not img:
            return Response({'detail': 'image file required.'}, status=400)
        if product.image:
            product.image.delete(save=False)
        product.image = img
        product.save(update_fields=['image', 'updated_at'])
        serializer = ProductDetailSerializer(product, context={'request': request})
        return Response({'image_url': serializer.data.get('image_url')})

    @action(detail=True, methods=['delete'], url_path='remove-image')
    def remove_image(self, request, pk=None):
        product = self.get_object()
        if product.image:
            product.image.delete(save=False)
            product.image = None
            product.save(update_fields=['image', 'updated_at'])
        return Response({'ok': True})

    # ── Showcase gallery: up to 5 photos + 1 video per product ──
    @action(detail=False, methods=['get'], url_path='media-specs')
    def media_specs(self, request):
        """The upload limits, so the UI note always matches what we enforce."""
        return Response(media_processing.upload_specs())

    @action(detail=True, methods=['post'], url_path='upload-media', parser_classes=[_MultiPartParser, _FormParser])
    def upload_media(self, request, pk=None):
        """Add one showcase asset. multipart/form-data: `file` + `kind`
        (image|video). Images become .webp, videos H.264 .mp4 — converted here,
        synchronously, before we respond. Enforces the per-product caps."""
        import os
        product = self.get_object()
        f = request.FILES.get('file')
        kind = (request.data.get('kind') or '').lower()
        if not f:
            return Response({'detail': 'file is required.'}, status=400)
        if kind not in ('image', 'video'):
            return Response({'detail': 'kind must be image or video.'}, status=400)

        ext = os.path.splitext(f.name)[1].lower()
        if kind == 'image':
            if ext and ext not in media_processing.ALLOWED_IMAGE_EXT:
                return Response({'detail': f'Unsupported image type ({ext}).'}, status=400)
            if product.media.filter(kind='image').count() >= media_processing.MAX_IMAGES:
                return Response({'detail': f'Up to {media_processing.MAX_IMAGES} photos only.'}, status=400)
        else:
            if ext and ext not in media_processing.ALLOWED_VIDEO_EXT:
                return Response({'detail': f'Unsupported video type ({ext}).'}, status=400)
            if product.media.filter(kind='video').exists():
                return Response({'detail': 'Only one video per product — remove the current one first.'}, status=400)

        try:
            if kind == 'image':
                content, w, h = media_processing.process_image(f)
                m = ProductMedia(store=product.store, product=product, kind='image',
                                 width=w, height=h)
                m.file = content
            else:
                out = media_processing.process_video(f)
                m = ProductMedia(store=product.store, product=product, kind='video',
                                 width=out['width'], height=out['height'],
                                 duration=out['duration'])
                m.file = out['file']
                if out['poster']:
                    m.poster = out['poster']
        except media_processing.MediaError as e:
            return Response({'detail': str(e)}, status=400)

        # New asset goes to the end of its kind's order.
        last = product.media.order_by('-order').first()
        m.order = (last.order + 1) if last else 0
        m.save()
        return Response(ProductMediaSerializer(m, context={'request': request}).data, status=201)

    @action(detail=True, methods=['delete'], url_path='media/(?P<media_id>[^/.]+)')
    def delete_media(self, request, pk=None, media_id=None):
        product = self.get_object()
        m = product.media.filter(id=media_id).first()
        if not m:
            return Response({'detail': 'Not found.'}, status=404)
        m.delete()   # also removes the file + poster from disk
        return Response({'ok': True})

    @action(detail=True, methods=['post'], url_path='reorder-media')
    def reorder_media(self, request, pk=None):
        """Persist the gallery order. Body: {"order": [media_id, media_id, ...]}."""
        product = self.get_object()
        ids = request.data.get('order') or []
        if not isinstance(ids, list):
            return Response({'detail': 'order must be a list of media ids.'}, status=400)
        owned = {str(m.id): m for m in product.media.all()}
        with transaction.atomic():
            for i, mid in enumerate(ids):
                m = owned.get(str(mid))
                if m:
                    m.order = i
                    m.save(update_fields=['order'])
        media = product.media.all()
        return Response(ProductMediaSerializer(media, many=True, context={'request': request}).data)

    @action(detail=True, methods=['post'], url_path='showcase-settings')
    def showcase_settings(self, request, pk=None):
        """Set the two display radios: lead (video|slideshow) + autoplay (bool)."""
        product = self.get_object()
        lead = request.data.get('showcase_lead')
        autoplay = request.data.get('showcase_autoplay')
        fields = []
        if lead in ('video', 'slideshow'):
            product.showcase_lead = lead
            fields.append('showcase_lead')
        if isinstance(autoplay, bool):
            product.showcase_autoplay = autoplay
            fields.append('showcase_autoplay')
        if fields:
            product.save(update_fields=fields + ['updated_at'])
        return Response({'showcase_lead': product.showcase_lead,
                         'showcase_autoplay': product.showcase_autoplay})

    @action(detail=False, methods=['post'], url_path='import-memory-base')
    def import_memory_base(self, request):
        """Rough CSV import for the Memory Base reference pool (superfix §2.5).

        Accepts a multipart `file` (a .csv) or a JSON `{"csv": "..."}` body. The
        first row is a header; a `name` column is required. Any other column whose
        header matches an existing attribute's key OR display name (case-insensitive)
        is attached as that attribute. Entries are dedup'd by name via
        register_memory_base_entry — re-importing an existing name is a skip.
        Returns {created, skipped}.
        """
        import csv, io
        from .product_service import register_memory_base_entry
        store = request.user.store
        f = request.FILES.get('file')
        raw = f.read().decode('utf-8-sig') if f else (request.data.get('csv') or '')
        if not raw.strip():
            return Response({'detail': 'No CSV provided.'}, status=status.HTTP_400_BAD_REQUEST)

        defs = {}
        for d in AttributeDefinition.objects.filter(store=store):
            defs[d.key.lower()] = d
            defs[d.name.lower()] = d

        try:
            reader = csv.DictReader(io.StringIO(raw))
        except Exception:
            return Response({'detail': 'Could not parse the CSV.'}, status=status.HTTP_400_BAD_REQUEST)

        created = skipped = 0
        with transaction.atomic():
            for row in reader:
                name, attrs = '', []
                for col, val in row.items():
                    if col is None:
                        continue
                    key = col.strip().lower()
                    if key == 'name':
                        name = (val or '').strip()
                    elif val and (val := val.strip()) and key in defs:
                        attrs.append({'definition': str(defs[key].id), 'value': val})
                if not name:
                    continue
                exists = Product.objects.filter(
                    store=store, source=Product.Source.MEMORY_BASE, name__iexact=name,
                ).exists()
                register_memory_base_entry(store, name=name, attributes=attrs)
                skipped += 1 if exists else 0
                created += 0 if exists else 1
        log_activity(request=request, op_type=ActivityLog.OperationType.OTHER,
                     action=f"Imported {created} Memory Base entry(ies) from CSV ({skipped} already existed)")
        return Response({'created': created, 'skipped': skipped})

    @action(detail=False, methods=['post'], url_path='dedup-memory-base')
    def dedup_memory_base(self, request):
        """Remove duplicate Memory Base entries (same name) for this store, keeping
        the richest of each. Soft-delete, recoverable. Returns {removed}."""
        from .product_service import dedup_memory_base_for_store
        removed = dedup_memory_base_for_store(request.user.store)
        if removed:
            log_activity(request=request, op_type=ActivityLog.OperationType.OTHER,
                         action=f"Removed {removed} duplicate Memory Base entry(ies)")
        return Response({'removed': removed})

    @action(detail=True, methods=['post'])
    def toggle_ghost(self, request, pk=None):
        """Flip a single product's POS visibility (ghost / un-ghost)."""
        product = self.get_object()
        product.hide_from_pos = not product.hide_from_pos
        product.save(update_fields=['hide_from_pos', 'updated_at'])
        log_activity(request=request, op_type=ActivityLog.OperationType.OTHER,
                     action=f"{'Ghosted' if product.hide_from_pos else 'Un-ghosted'} product '{product.name}'")
        return Response({'id': str(product.id), 'hide_from_pos': product.hide_from_pos})

    # ── Price stickers from the product list (s167, PROLINE-PARITY Upgrade 14) ──
    LABEL_MAX_PRODUCTS = 2000

    @action(detail=False, methods=['post'], url_path='label-data')
    def label_data(self, request):
        """Sticker payload for chosen products — the same item shape as a purchase's label-data
        (`PurchaseInvoiceViewSet.label_data`), so the one label print page serves both.
        Body: {ids: [product ids]}. One item per live variant, with its shop-wide stock.
        `quantity` = 1 label per variant; `stock_labels` = one per unit in stock (min 1), except a
        product sold by weight, which always gets 1 (kg are not countable stickers)."""
        ids = request.data.get('ids')
        if not isinstance(ids, list) or not ids:
            return Response({'error': 'ids must be a non-empty list.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(ids) > self.LABEL_MAX_PRODUCTS:
            return Response({'error': f'At most {self.LABEL_MAX_PRODUCTS} products at a time.'},
                            status=status.HTTP_400_BAD_REQUEST)
        store = request.user.store
        from django.db.models import Prefetch
        from inventory.sku import sku2_active
        from core.models import StoreSettings
        st, _ = StoreSettings.objects.get_or_create(store_id=store.pk)   # fresh: sudo may have just changed SKU2
        with_sku2 = sku2_active(store) and st.sku2_print_label
        variants_qs = (ProductVariant.objects.filter(store=store)
                       .annotate(_stock=Coalesce(Sum('stock_levels__quantity'), Value(Decimal('0')),
                                                 output_field=DecimalField(max_digits=14, decimal_places=3)))
                       .prefetch_related('attributes')
                       .order_by('created_at'))
        products = (Product.objects.filter(store=store, id__in=ids, source=Product.Source.STORE)
                    .select_related('category__parent__parent__parent')   # spec-set chain, no per-row query
                    .prefetch_related(Prefetch('variants', queryset=variants_qs))
                    .order_by('name'))
        from .spec_sets import SpecLineBuilder
        spec_lines = SpecLineBuilder()   # Upgrade 9: the category's specs on one line (printed if the preset says so)
        items = []
        for p in products:
            variants = list(p.variants.all())
            by_weight = p.selling_mode == Product.SellingMode.WEIGHT
            for v in variants:
                name = p.name
                if len(variants) > 1:   # say WHICH variant on the sticker
                    vals = [a.value for a in v.attributes.all() if a.value]
                    if vals:
                        name = f"{p.name} · {' / '.join(vals)}"
                stock = v._stock or Decimal('0')
                items.append({
                    'variant_id':   str(v.id),
                    'product_id':   str(p.id),
                    'product_name': name,
                    'sku':          v.sku,
                    'sku2':         (v.sku2 or '') if with_sku2 else '',
                    'barcode':      v.barcode or v.sku,
                    'sell_price':   str(v.sell_price),
                    'specs':        spec_lines.line(p.category, v.attributes.all()),
                    'stock':        str(stock),
                    'by_weight':    by_weight,
                    'quantity':     1,
                    'stock_labels': 1 if by_weight else max(1, int(stock)),
                })
        return Response({'store_name': store.name, 'items': items})

    @action(detail=False, methods=['post'])
    def bulk_ghost(self, request):
        """Set POS visibility on many products at once. Body: {ids, hide}."""
        qs = self._store_products(request.data.get('ids'))
        if qs is None:
            return Response({'error': 'ids must be a non-empty list.'}, status=status.HTTP_400_BAD_REQUEST)
        hide = bool(request.data.get('hide', True))
        from search.indexing import sync_products_on_commit
        with transaction.atomic():
            ids = list(qs.values_list('id', flat=True))
            count = qs.update(hide_from_pos=hide)
            # .update() skips the post_save signal → re-sync these products' search docs (B8).
            sync_products_on_commit(ids)
        log_activity(request=request, op_type=ActivityLog.OperationType.OTHER,
                     action=f"Bulk {'ghosted' if hide else 'un-ghosted'} {count} product(s)")
        return Response({'updated': count, 'hide_from_pos': hide})

    @action(detail=False, methods=['post'])
    def bulk_update(self, request):
        """Limited bulk edit — retail price and/or category only (s18 decision).

        Body: {ids, retail_price?, category?}. retail_price is applied to every
        variant's sell_price; category is set on the product itself.
        """
        qs = self._store_products(request.data.get('ids'))
        if qs is None:
            return Response({'error': 'ids must be a non-empty list.'}, status=status.HTTP_400_BAD_REQUEST)

        retail_raw = request.data.get('retail_price', None)
        category_id = request.data.get('category', None)  # '' / None = leave unchanged

        retail = None
        if retail_raw not in (None, ''):
            try:
                retail = Decimal(str(retail_raw))
                if retail < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                return Response({'error': 'retail_price must be a non-negative number.'},
                                status=status.HTTP_400_BAD_REQUEST)

        category = None
        if category_id:
            category = Category.objects.filter(store=request.user.store, id=category_id).first()
            if category is None:
                return Response({'error': 'Category not found in this store.'},
                                status=status.HTTP_400_BAD_REQUEST)

        if retail is None and category is None and not category_id:
            return Response({'error': 'Nothing to update — provide retail_price and/or category.'},
                            status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            products = list(qs)
            if category is not None:
                qs.update(category=category)
            if retail is not None:
                ProductVariant.objects.filter(
                    product__in=products, product__store=request.user.store
                ).update(sell_price=retail)
        log_activity(request=request, op_type=ActivityLog.OperationType.OTHER,
                     action=f"Bulk edited {len(products)} product(s)"
                     + (f" — retail={retail}" if retail is not None else '')
                     + (f" — category={category.name}" if category is not None else ''))
        return Response({'updated': len(products)})

    @action(detail=False, methods=['post'])
    def bulk_delete(self, request):
        """Soft-delete many products with an audit reason. Body: {ids, reason, note}."""
        qs = self._store_products(request.data.get('ids'))
        if qs is None:
            return Response({'error': 'ids must be a non-empty list.'}, status=status.HTTP_400_BAD_REQUEST)

        reason = (request.data.get('reason') or '').strip().upper()
        valid = {c for c, _ in Product.DeleteReason.choices}
        if reason not in valid:
            return Response({'error': f'reason must be one of {sorted(valid)}.'},
                            status=status.HTTP_400_BAD_REQUEST)
        note = (request.data.get('note') or '').strip()[:255]

        with transaction.atomic():
            products = list(qs)
            for p in products:
                p.is_deleted   = True
                p.deleted_at   = timezone.now()
                p.delete_reason = reason
                p.delete_note   = note
                p.deleted_by    = request.user
                p.save(update_fields=['is_deleted', 'deleted_at', 'delete_reason',
                                      'delete_note', 'deleted_by', 'updated_at'])
        log_activity(request=request, op_type=ActivityLog.OperationType.OTHER,
                     action=f"Bulk deleted {len(products)} product(s) — reason: {reason}")
        return Response({'deleted': len(products)})

    @action(detail=False, methods=['get'], url_path='autocomplete')
    def autocomplete(self, request):
        """Fast ranked product search for the New Purchase / New Product autocomplete.

        Behaviour is governed by store.settings.autocomplete_source:
        - memory_base (default): searches STORE + MEMORY_BASE; STORE entries
          surfaced first (via _src_rank annotation) when names collide.
        - store_history: searches only STORE products; sets no_history=true in
          the response when the result set is empty so the frontend can hint.

        Returns {results: [...], no_history: bool}.

        Query params:
          ?q=<text>   required, minimum 3 characters
        """
        from django.db.models import Case, When, IntegerField, Value
        q = (request.query_params.get('q') or '').strip()
        # POS mode (§POS-AR): the scanner-first sell screen. STORE products only
        # (Memory Base reference entries are never sellable), hidden products
        # excluded, and a shorter 2-char trigger so a short query / partial scan
        # still searches.
        pos_mode = (request.query_params.get('pos') or '').lower() in ('1', 'true')
        if len(q) < (2 if pos_mode else 3):
            return Response({'results': [], 'no_history': False})

        store = request.user.store
        if pos_mode:
            ac_source = 'store_history'   # STORE-only base filter + ranking
        else:
            try:
                ac_source = store.settings.autocomplete_source
            except Exception:
                ac_source = 'memory_base'
        store_history = (ac_source == 'store_history')

        base = Product.objects.filter(store=store).select_related(
            'category', 'category__parent',
            'category__parent__parent', 'category__parent__parent__parent',
        ).prefetch_related(
            'supplier',
            'variants', 'variants__stock_levels',
            'variants__attributes', 'variants__attributes__definition',
        )
        if store_history:
            base = base.filter(source=Product.Source.STORE)
        if pos_mode:
            base = base.exclude(hide_from_pos=True)

        # --- Typesense first: typo-tolerant ranked IDs, then load + serialize from
        #     the DB so the JSON shape is identical to the pg_trgm path. Any error
        #     (service down/slow/unconfigured) → fall through to pg_trgm below. ---
        results = None
        from search import client as ts
        if ts.is_configured():
            try:
                ids = ts.search_ids(str(store.id), q, store_history=store_history, pos=pos_mode)
                by_id = {str(p.id): p for p in base.filter(id__in=ids)}
                results = [by_id[i] for i in ids if i in by_id]
            except Exception:
                results = None

        # --- pg_trgm fallback (also the path when Typesense is unconfigured). ---
        if results is None:
            qs = base.filter(
                Q(name__icontains=q) |
                Q(
                    variants__attributes__definition__key__in=['active_ing', 'active_ing_ar'],
                    variants__attributes__value__icontains=q,
                )
            ).distinct()

            if ac_source == 'memory_base':
                qs = qs.annotate(
                    _rank=Case(
                        When(name__istartswith=q, then=Value(0)),
                        When(name__icontains=q,   then=Value(1)),
                        default=Value(2),
                        output_field=IntegerField(),
                    ),
                    _src_rank=Case(
                        When(source=Product.Source.STORE, then=Value(0)),
                        default=Value(1),
                        output_field=IntegerField(),
                    ),
                ).order_by('_rank', '_src_rank', 'name')[:20]
            else:
                qs = qs.annotate(
                    _rank=Case(
                        When(name__istartswith=q, then=Value(0)),
                        When(name__icontains=q,   then=Value(1)),
                        default=Value(2),
                        output_field=IntegerField(),
                    ),
                ).order_by('_rank', 'name')[:20]
            results = list(qs)

        # POS scanner: SKU + barcode aren't in the Typesense index — match them
        # directly in the DB and surface them FIRST (a scan should always beat a
        # fuzzy name hit), then fall back to the name/ingredient matches.
        # s159: an EXACT code (SKU / SKU2 / barcode, dashes ignored) comes before one that merely
        # contains the typed digits — `12077` must land on 12077, not on 1207740010.
        # A pack/box barcode counts as a code too (only while the shop sells in packs) — the
        # matching pack is named in `scan_unit` so the POS adds 1 pack, not 1 piece.
        exact_ids = set()
        if pos_mode:
            from inventory.code_search import exact_code, code_contains
            from inventory.models import is_multi_unit_enabled
            from inventory.sku import sku2_active
            with_sku2 = sku2_active(store)
            packs = is_multi_unit_enabled(store.id)
            exact_hits = list(base.filter(exact_code(q, with_sku2, packs))[:20])
            exact_ids = {p.id for p in exact_hits}
            seen = set(exact_ids)
            sku_hits = exact_hits + [p for p in base.filter(code_contains(q, with_sku2, packs))[:20]
                                     if p.id not in seen]
            seen = {p.id for p in sku_hits}
            results = (sku_hits + [p for p in results if p.id not in seen])[:20]

        # s166 (Yakot): a Memory Base copy of a product the shop ALREADY has is hidden — it showed right
        # under the shop's own row with identical text, and picking it went down the "new product" path
        # (a duplicate on receive). Same rule as the Memory Base itself: case-insensitive name.
        if not store_history:
            from django.db.models.functions import Lower, Trim
            mb_names = {(p.name or '').strip().lower() for p in results if p.source == Product.Source.MEMORY_BASE}
            if mb_names:
                owned = set(Product.objects.filter(store=store, source=Product.Source.STORE)
                            .annotate(_ln=Lower(Trim('name'))).filter(_ln__in=mb_names)
                            .values_list('_ln', flat=True))
                results = [p for p in results
                           if not (p.source == Product.Source.MEMORY_BASE and (p.name or '').strip().lower() in owned)]

        no_history = (store_history and len(results) == 0)
        data = ProductListSerializer(results, many=True, context={'request': request}).data
        if pos_mode:
            from inventory.code_search import scan_unit_id
            for p, row in zip(results, data):
                row['scan_unit'] = scan_unit_id(row.get('selling_units'), q) if p.id in exact_ids else None
        return Response({'results': data, 'no_history': no_history})

    # strength tokens to drop when matching on ingredient NAME ("Acetazolamide 250 mg"
    # → "Acetazolamide"). Only number+unit pairs are removed, so bare names are untouched.
    _STRENGTH_RE = re.compile(r'\d+(?:[.,]\d+)?\s*(?:mg|mcg|µg|gm|g|ml|iu|units?|%)\b', re.I)
    _AR_RE = re.compile(r'[؀-ۿ]')

    @action(detail=False, methods=['get'], url_path='alternatives')
    def alternatives(self, request):
        """Same-active-ingredient discovery for the /sameing and /sametrade search flags.

        - mode=sametrade: q IS the active ingredient → list every product containing it.
        - mode=sameing:   q is a TRADE NAME → resolve it to a product, read its active
          ingredient, strip the strength, then run the same containment search.

        Scope: pos=1 → STORE inventory, in-stock only (the sell counter — out-of-stock
        substitution). Otherwise the Memory Base reference catalog (no stock filter).
        Arabic query → match the Arabic ingredient (active_ing_ar).

        Matching is a case-insensitive CONTAINS on the ingredient NAME; strength is
        shown per row but never filters (a 250mg brand still surfaces for a 500mg search).
        Returns {results: [...], ingredient: "<resolved ingredient>"}.

        ponytail: CONTAINS on raw free-text — spelling variants ("cystiene"/"cysteine")
        and A+B combos won't group perfectly. Data-quality ceiling; the human picks from
        the list. Upgrade path: a normalized ingredient FK when §DE enrichment lands.
        """
        q = (request.query_params.get('q') or '').strip()
        mode = (request.query_params.get('mode') or 'sameing').lower()
        pos_mode = (request.query_params.get('pos') or '').lower() in ('1', 'true')
        if len(q) < 2:
            return Response({'results': [], 'ingredient': ''})

        store = request.user.store
        is_ar = bool(self._AR_RE.search(q))
        ing_key = 'active_ing_ar' if is_ar else 'active_ing'

        # /sameing resolves the trade name against the FULL catalog (store + memory base,
        # ignoring stock) — the searched brand is often the out-of-stock one.
        if mode == 'sameing':
            # Resolve the trade name against the full catalog. Also match the Arabic
            # brand attribute (brand_ar) so an Arabic-typed brand still resolves — the
            # product `name` itself is English.
            src = (Product.objects.filter(store=store)
                   .filter(Q(name__istartswith=q) | Q(name__icontains=q) |
                           Q(variants__attributes__definition__key='brand_ar',
                             variants__attributes__value__icontains=q))
                   .order_by('name').first())
            ingredient = ''
            if src:
                pa = (ProductAttribute.objects
                      .filter(variant__product=src, definition__key=ing_key)
                      .exclude(value='').first())
                if pa:
                    ingredient = self._STRENGTH_RE.sub('', pa.value).strip(' +-.,')
            if not ingredient:
                return Response({'results': [], 'ingredient': ''})
        else:  # sametrade — q is the ingredient itself
            ingredient = q

        base = (Product.objects.filter(store=store)
                .select_related('category', 'drug_profile', 'supplier')
                .prefetch_related(
                    'variants', 'variants__stock_levels', 'variants__selling_units',
                    'variants__attributes', 'variants__attributes__definition',
                ))
        if pos_mode:
            base = base.filter(source=Product.Source.STORE).exclude(hide_from_pos=True)
        else:
            base = base.filter(source=Product.Source.MEMORY_BASE)

        matches = list(base.filter(
            variants__attributes__definition__key=ing_key,
            variants__attributes__value__icontains=ingredient,
        ).distinct().order_by('name')[:100])

        serializer = ProductListSerializer(matches, many=True, context={'request': request})
        return Response({'results': serializer.data, 'ingredient': ingredient})


class ProductVariantViewSet(_AtomicWritesMixin, viewsets.ModelViewSet):
    serializer_class = ProductVariantSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
    }

    def get_queryset(self):
        return ProductVariant.objects.filter(
            product__store=self.request.user.store
        ).prefetch_related('attributes', 'attributes__definition', 'stock_levels')


class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
        'contents':       'MANAGER',
        'resolve_delete': 'ADMIN',
        # Spec sets (Upgrade 9, s168). Reading = anyone who reads categories (the product form needs it);
        # editing a set = same as editing a category; a preset may CREATE attributes = same as attributes.
        'specs':          'CASHIER',
        'set_specs':      'MANAGER',
        'spec_presets':   'MANAGER',
        'apply_preset':   'ADMIN',
    }

    def get_queryset(self):
        from django.db.models import Count
        return Category.objects.filter(store=self.request.user.store).annotate(
            spec_count=Count('spec_items', filter=Q(spec_items__is_deleted=False,
                                                    spec_items__definition__is_deleted=False)))

    # ── Spec sets (PROLINE-PARITY Upgrade 9, s168) — logic in inventory/spec_sets.py ──
    def _specs_payload(self, category):
        from .spec_sets import effective_spec_set
        effective = effective_spec_set(category)
        return {
            'category': str(category.id),
            'effective': effective,                                   # what the product form asks for
            'own': [s for s in effective if s['inherited_from'] is None],
            'inherited': [s for s in effective if s['inherited_from'] is not None],
        }

    @action(detail=True, methods=['get'])
    def specs(self, request, pk=None):
        """The category's spec set: `effective` = parents' lines (root first) + its own."""
        return Response(self._specs_payload(self.get_object()))

    @specs.mapping.put
    def set_specs(self, request, pk=None):
        """Replace the category's OWN lines. Body: {items: [{definition, required}]} in order."""
        from .spec_sets import set_own_specs
        category = self.get_object()
        set_own_specs(category, request.data.get('items'))
        return Response(self._specs_payload(category))

    @action(detail=False, methods=['get'], url_path='spec-presets')
    def spec_presets(self, request):
        """The built-in starter spec sets (Laptop, Phone, Monitor…), both languages."""
        from .spec_presets import preset_catalog
        return Response(preset_catalog())

    @action(detail=True, methods=['post'], url_path='apply-preset')
    def apply_preset(self, request, pk=None):
        """Attach a starter preset. Body: {preset: 'laptop'}. Creates missing attributes; never duplicates."""
        from .spec_sets import apply_preset
        category = self.get_object()
        result = apply_preset(category, request.data.get('preset'))
        return Response({**self._specs_payload(category), **result})

    def _descendant_ids(self, category):
        """Ids of every active category below `category` (not including itself)."""
        ids, frontier = [], [category.id]
        while frontier:
            kids = list(Category.objects.filter(parent_id__in=frontier)
                        .values_list('id', flat=True))
            ids.extend(kids)
            frontier = kids
        return ids

    @action(detail=True, methods=['get'])
    def contents(self, request, pk=None):
        """What's inside a category — drives the delete modal (move vs purge)."""
        category = self.get_object()
        desc_ids = self._descendant_ids(category)
        product_count = Product.objects.filter(
            category_id__in=[category.id] + desc_ids).count()
        parent = category.parent
        return Response({
            'id': str(category.id),
            'name': category.name,
            'parent': {'id': str(parent.id), 'name': parent.name} if parent else None,
            'subcategory_count': len(desc_ids),
            'product_count': product_count,
        })

    @action(detail=True, methods=['post'], url_path='resolve-delete')
    def resolve_delete(self, request, pk=None):
        """Delete a non-empty category. mode='move' lifts contents up to the
        parent; mode='purge' soft-deletes the whole branch + its products
        (an inventory write-off, so a reason is required)."""
        from rest_framework.exceptions import ValidationError as DRFValidationError
        category = self.get_object()
        mode = request.data.get('mode')
        if mode not in ('move', 'purge'):
            raise DRFValidationError({'mode': "Must be 'move' or 'purge'."})

        with transaction.atomic():
            if mode == 'move':
                parent = category.parent      # None for a top-level category
                Product.objects.filter(category=category).update(category=parent)
                Category.objects.filter(parent=category).update(parent=parent)
                category.delete()             # soft
                return Response({'detail': 'Contents moved up; category deleted.'})

            # purge
            reason = (request.data.get('reason') or '').strip()
            note = (request.data.get('note') or '').strip()[:255]
            if reason not in dict(Product.DeleteReason.choices):
                raise DRFValidationError(
                    {'reason': 'A valid reason is required to delete a category with its items.'})
            desc_ids = self._descendant_ids(category)
            now = timezone.now()
            doomed = Product.objects.filter(category_id__in=[category.id] + desc_ids)
            doomed_ids = list(doomed.values_list('id', flat=True))
            doomed.update(
                is_deleted=True, deleted_at=now,
                delete_reason=reason, delete_note=note, deleted_by=request.user,
            )
            # .update() skips the post_save signal → drop these products from search (B8).
            from search.indexing import sync_products_on_commit
            sync_products_on_commit(doomed_ids)
            Category.objects.filter(id__in=desc_ids).update(is_deleted=True, deleted_at=now)
            category.delete()                 # soft
            return Response({'detail': 'Category and its contents deleted.'})

    def _save_or_400(self, serializer, **kwargs):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from rest_framework.exceptions import ValidationError as DRFValidationError
        try:
            serializer.save(**kwargs)
        except DjangoValidationError as exc:
            # depth / cycle guard from Category.clean() -> clean 400
            raise DRFValidationError(getattr(exc, 'message_dict', None) or exc.messages)

    def perform_create(self, serializer):
        self._save_or_400(serializer, store=self.request.user.store)

    def perform_update(self, serializer):
        self._save_or_400(serializer)

    def perform_destroy(self, instance):
        from rest_framework.exceptions import ValidationError as DRFValidationError
        if Category.objects.filter(parent=instance).exists():
            raise DRFValidationError(
                {'detail': 'This category has sub-categories. Move or delete them first.'})
        instance.delete()


def supplier_account_sources(store):
    """The two querysets a supplier's RUNNING ACCOUNT is built from (Yakot's call, s127):
    payable = Σ RECEIVED purchases' total − Σ payments made to the supplier.

    ONE definition, shared by the suppliers-list balance (SupplierViewSet.get_queryset) and the
    account statement (SupplierViewSet.statement), so the two can never drift apart. A DRAFT
    purchase isn't a liability yet (matches the AP-aging report); soft-deleted rows don't count.
    There are no purchase returns and no supplier opening balance in the system yet (checked s167) —
    when either is built, add it HERE so the list and the statement both pick it up."""
    from finance.models import PurchaseInvoice, SupplierPayment
    purchases = PurchaseInvoice.objects.filter(
        store=store, status=PurchaseInvoice.Status.RECEIVED, is_deleted=False)
    payments = SupplierPayment.objects.filter(store=store, is_deleted=False)
    return purchases, payments


class SupplierViewSet(viewsets.ModelViewSet):
    serializer_class = SupplierSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'MANAGER',
        'retrieve':       'MANAGER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'ADMIN',
        'purchases':      'MANAGER',
        'statement':      'MANAGER',
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'contact_info']
    ordering_fields = ['name', 'contact_info', 'balance', 'created_at']
    ordering = ['name']

    def get_queryset(self):
        # Running-account payable (s127): balance = Σ(RECEIVED purchases' total)
        # − Σ(payments to this supplier). Only RECEIVED purchases are a real
        # liability — a DRAFT hasn't been received, so it's excluded (matches the
        # AP-aging report). Two independent Subqueries avoid the join-multiplication
        # that a single annotate over both reverse relations would cause.
        # The two sources live in supplier_account_sources() — shared with `statement`.
        store = self.request.user.store
        DEC = DecimalField(max_digits=12, decimal_places=2)
        purchases, payments = supplier_account_sources(store)
        received = (purchases.filter(supplier=OuterRef('pk'))
                    .values('supplier').annotate(t=Sum('total_amount')).values('t'))
        paid = (payments.filter(supplier=OuterRef('pk'))
                .values('supplier').annotate(t=Sum('amount')).values('t'))
        return (
            Supplier.objects
            .filter(store=store)
            .annotate(
                received_total=Coalesce(Subquery(received, output_field=DEC),
                                        Value(Decimal('0')), output_field=DEC),
                payments_total=Coalesce(Subquery(paid, output_field=DEC),
                                        Value(Decimal('0')), output_field=DEC),
                balance=ExpressionWrapper(F('received_total') - F('payments_total'),
                                          output_field=DEC),
            )
        )

    def perform_create(self, serializer):
        # Lock the prefix on creation — irreversible by design
        serializer.save(store=self.request.user.store, prefix_locked=True)

    def perform_update(self, serializer):
        instance = self.get_object()
        if instance.prefix_locked and 'code_prefix' in serializer.validated_data:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'code_prefix': 'Supplier prefix is locked and cannot be changed.'})
        serializer.save()

    @action(detail=True, methods=['get'])
    def purchases(self, request, pk=None):
        from rest_framework import serializers as drf_serializers
        from finance.models import PurchaseInvoice
        from rest_framework.pagination import PageNumberPagination
        supplier = self.get_object()
        qs = (PurchaseInvoice.objects
              .filter(store=request.user.store, supplier=supplier, is_deleted=False)
              .order_by('-date'))

        class PurchaseSummarySerializer(drf_serializers.ModelSerializer):
            class Meta:
                from finance.models import PurchaseInvoice as M
                model = M
                fields = ['id', 'vendor_reference', 'status', 'date', 'total_amount', 'paid_amount', 'notes', 'created_at']

        pager = PageNumberPagination()
        pager.page_size = 20
        page = pager.paginate_queryset(qs, request)
        return pager.get_paginated_response(PurchaseSummarySerializer(page, many=True).data)

    @action(detail=True, methods=['get'])
    def statement(self, request, pk=None):
        """GET /api/inventory/suppliers/<id>/statement/?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
        The supplier's account statement (كشف حساب): every purchase and payment on their running
        account, oldest first, with a running balance.

        Shop's point of view (a payable account): a RECEIVED purchase is a CREDIT (we owe more),
        a payment to the supplier is a DEBIT (we owe less). balance = credit − debit, so a positive
        balance = money the shop still owes the supplier (same sign as the suppliers list).

        opening_balance = everything dated before date_from; lines = date_from..date_to (both
        inclusive, shop-local dates); closing_balance = opening + the lines. With no date_to the
        closing balance equals the list's `balance` (same sources — supplier_account_sources)."""
        from django.utils.dateparse import parse_date
        supplier = self.get_object()   # store-scoped via get_queryset → another shop's supplier = 404
        store = request.user.store

        dates = {}
        for key in ('date_from', 'date_to'):
            raw = (request.query_params.get(key) or '').strip()
            try:
                dates[key] = parse_date(raw) if raw else None
            except ValueError:   # well-formed but impossible, e.g. 2026-02-31
                dates[key] = None
            if raw and dates[key] is None:
                return Response({key: 'Use a date like 2026-01-31.'}, status=status.HTTP_400_BAD_REQUEST)
        df, dt = dates['date_from'], dates['date_to']
        if df and dt and df > dt:
            return Response({'date_to': 'The end date is before the start date.'},
                            status=status.HTTP_400_BAD_REQUEST)

        ZERO = Decimal('0.00')
        purchases, payments = supplier_account_sources(store)
        entries = []
        for p in purchases.filter(supplier=supplier).order_by():
            entries.append({
                'date': p.date, 'created_at': p.created_at, 'kind': 'purchase',
                'ref': p.purchase_number or p.vendor_reference or '',
                'vendor_reference': p.vendor_reference or '', 'note': p.notes or '',
                'debit': ZERO, 'credit': p.total_amount or ZERO, 'link_id': str(p.id),
            })
        for pay in payments.filter(supplier=supplier).select_related('payment_method').order_by():
            entries.append({
                'date': pay.date, 'created_at': pay.created_at, 'kind': 'payment',
                'ref': pay.note or '', 'note': '',
                # PROLINE-PARITY Upgrade 6: the shop's own method name; an old payment the migration could
                # not map has payment_method = None and its name falls back to the old code's label
                # (same rule as SupplierPaymentSerializer.payment_method_name).
                'payment_method': str(pay.payment_method_id) if pay.payment_method_id else None,
                'payment_method_name': (pay.payment_method.name if pay.payment_method_id
                                        else pay.get_method_display()),
                'method': pay.method, 'method_display': pay.get_method_display(),
                'debit': pay.amount or ZERO, 'credit': ZERO, 'link_id': str(pay.id),
            })
        entries.sort(key=lambda e: (e['date'], e['created_at']))

        def money(v):
            return str(Decimal(v).quantize(Decimal('0.01')))

        opening = ZERO
        running = ZERO
        tot_debit = ZERO
        tot_credit = ZERO
        lines = []
        for e in entries:
            day = timezone.localtime(e['date']).date()
            effect = e['credit'] - e['debit']
            if df and day < df:
                opening += effect
                running = opening
                continue
            if dt and day > dt:
                break   # sorted by date — nothing after this can be in range
            running += effect
            tot_debit += e['debit']
            tot_credit += e['credit']
            line = {k: v for k, v in e.items() if k != 'created_at'}
            line.update(debit=money(e['debit']), credit=money(e['credit']),
                        running_balance=money(running))
            lines.append(line)

        return Response({
            'supplier': {'id': str(supplier.id), 'name': supplier.name,
                         'company_name': supplier.company_name,
                         'code_prefix': supplier.code_prefix,
                         'phone_number': supplier.phone_number},
            'date_from': df, 'date_to': dt,
            'opening_balance': money(opening),
            'lines': lines,
            'totals': {'debit': money(tot_debit), 'credit': money(tot_credit)},
            'closing_balance': money(running),
            # The live balance the suppliers list shows (as of now — ignores the dates); a cross-check.
            'current_balance': money(supplier.balance),
        })


class SupplierPrefixCheckView(APIView):
    """GET /api/inventory/suppliers/check-prefix/?prefix=101 — store-scoped availability check.
    No `prefix` → just the width a new code needs + the next free code (s159 SKU setup)."""
    permission_classes = [IsAuthenticated, IsManagerOrAbove]

    def get(self, request):
        from inventory.sku import supplier_code_width, next_free_supplier_code
        store = request.user.store
        width = supplier_code_width(store)
        info = {'width': width, 'next_free': next_free_supplier_code(store)}
        prefix = request.query_params.get('prefix', '').strip()
        if not prefix:
            return Response(info)
        if not prefix.isdigit() or len(prefix) != width or prefix[0] == '0':
            return Response({**info, 'detail': f'Provide a {width}-digit code (not starting with 0).'},
                            status=status.HTTP_400_BAD_REQUEST)
        taken = Supplier.all_objects.filter(store=store, code_prefix=prefix).exists()
        return Response({**info, 'prefix': prefix, 'available': not taken})


class TaxViewSet(viewsets.ModelViewSet):
    serializer_class = TaxSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'ADMIN',
        'update':         'ADMIN',
        'partial_update': 'ADMIN',
        'destroy':        'ADMIN',
    }

    def get_queryset(self):
        return Tax.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class StockAdjustmentViewSet(viewsets.ModelViewSet):
    serializer_class = StockAdjustmentSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':     'MANAGER',
        'retrieve': 'MANAGER',
        'create':   'MANAGER',
    }
    http_method_names = ['get', 'post', 'head', 'options']  # immutable ledger — no edit/delete

    def get_queryset(self):
        return (
            StockAdjustment.objects
            .filter(store=self.request.user.store)
            .select_related('variant__product', 'branch', 'adjusted_by')
            .order_by('-created_at')
        )

    def perform_create(self, serializer):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from rest_framework.exceptions import ValidationError as DRFValidationError
        try:
            adjustment = serializer.save(store=self.request.user.store, adjusted_by=self.request.user)
        except DjangoValidationError as exc:
            # Negative-stock policy block (raised in StockAdjustment.save) -> clean 400.
            raise DRFValidationError({'detail': exc.messages})
        log_activity(
            request=self.request,
            action=f"Stock adjustment: {adjustment.variant.sku} ({adjustment.quantity_change:+})",
            op_type=ActivityLog.OperationType.ADJUSTMENT,
            details={
                'adjustment_id': str(adjustment.id),
                'sku': adjustment.variant.sku,
                'product': adjustment.variant.product.name,
                'change': str(adjustment.quantity_change),
                'reason': adjustment.reason,
            },
        )


class StockTransferViewSet(viewsets.ModelViewSet):
    serializer_class = StockTransferSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':     'ADMIN',
        'retrieve': 'ADMIN',
        'create':   'ADMIN',
    }
    http_method_names = ['get', 'post', 'head', 'options']  # immutable ledger

    def get_queryset(self):
        return (
            StockTransfer.objects
            .filter(store=self.request.user.store)
            .prefetch_related('items__variant__product')
            .select_related('from_branch', 'to_branch', 'transferred_by')
        )

    def perform_create(self, serializer):
        transfer = serializer.save(
            store=self.request.user.store,
            transferred_by=self.request.user,
        )
        log_activity(
            request=self.request,
            action=f"Stock transfer: {transfer.from_branch.name} → {transfer.to_branch.name} ({transfer.items.count()} item(s))",
            op_type=ActivityLog.OperationType.ADJUSTMENT,
            details={
                'transfer_id': str(transfer.id),
                'from_branch': transfer.from_branch.name,
                'to_branch': transfer.to_branch.name,
                'items': [
                    {'sku': i.variant.sku, 'qty': str(i.quantity)}
                    for i in transfer.items.all()
                ],
            },
        )


# ── Catalog Import / Export ─────────────────────────────────────────────
from django.http import HttpResponse
from rest_framework.parsers import MultiPartParser, FormParser
from .import_export import CatalogImporter, parse_csv, export_catalog


class _CatalogImportBase(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrAbove]
    parser_classes = [MultiPartParser, FormParser]

    def _read(self, request):
        f = request.FILES.get('file')
        if not f:
            return None, Response({'detail': 'A CSV file is required.'},
                                  status=status.HTTP_400_BAD_REQUEST)
        if not (f.name or '').lower().endswith('.csv'):
            return None, Response({'detail': 'Only .csv files are accepted.'},
                                  status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
        if f.size and f.size > 5 * 1024 * 1024:
            return None, Response({'detail': 'File too large (max 5 MB).'},
                                  status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        return f.read(), None


class CatalogImportValidateView(_CatalogImportBase):
    """POST a CSV → validate only (no writes). Returns errors / warnings / summary."""
    def post(self, request):
        raw, err = self._read(request)
        if err:
            return err
        headers, rows = parse_csv(raw)
        result = CatalogImporter(request.user.store, request.user).validate(headers, rows)
        return Response(result, status=status.HTTP_200_OK if result['ok'] else status.HTTP_400_BAD_REQUEST)


class CatalogImportCommitView(_CatalogImportBase):
    """POST a CSV → validate + import in one transaction (rejects on any error)."""
    def post(self, request):
        raw, err = self._read(request)
        if err:
            return err
        headers, rows = parse_csv(raw)
        result = CatalogImporter(request.user.store, request.user).commit(headers, rows)
        if not result['ok']:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        s = result['summary']
        # ONE entry per import (s167): the counts + the row-by-row price/name changes (old → new) in details
        log_activity(
            request=request,
            action=(f"Catalog CSV import: {s.get('created', 0)} new products, {s.get('updated', 0)} updated "
                    f"({s.get('price_changes', 0)} price, {s.get('name_changes', 0)} name changes), "
                    f"{s.get('unchanged', 0)} unchanged"),
            op_type=ActivityLog.OperationType.OTHER,
            details=result['summary'],
        )
        return Response(result)


class CatalogExportView(APIView):
    """GET → the store's catalog as a CSV download (same schema as import)."""
    permission_classes = [IsAuthenticated, IsManagerOrAbove]

    def get(self, request):
        csv_text = export_catalog(request.user.store)
        resp = HttpResponse(csv_text, content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="catalog_export.csv"'
        return resp


# ── Storage (operational visibility filter) ─────────────────────────────
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from core.models import Branch


class StorageLocationViewSet(viewsets.ModelViewSet):
    """CRUD for storage locations + a `contents` action listing parked layers."""
    serializer_class = StorageLocationSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'contents':       'CASHIER',
        'create':         'MANAGER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'MANAGER',
    }

    def get_queryset(self):
        return StorageLocation.objects.filter(store=self.request.user.store)

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)

    @action(detail=True, methods=['get'])
    def contents(self, request, pk=None):
        """Active (non-emptied) cost layers parked at this location."""
        location = self.get_object()
        layers = (
            StorageStock.objects
            .filter(store=request.user.store, storage_location=location)
            .select_related('variant__product')
            .order_by('variant__sku', 'moved_in_at')
        )
        return Response(StorageStockSerializer(layers, many=True, context={"request": request}).data)


class _StorageOpsBase(APIView):
    """Shared helpers for the storage move/retrieve/write-off endpoints."""
    permission_classes = [IsAuthenticated, IsManagerOrAbove]

    def _store(self):
        return self.request.user.store

    def _variant(self, data):
        v = ProductVariant.objects.filter(
            id=data.get('variant'), product__store=self._store()
        ).first()
        if not v:
            raise DRFValidationError({'variant': 'Unknown or out-of-store variant.'})
        return v

    def _location(self, data):
        loc = StorageLocation.objects.filter(
            id=data.get('storage_location'), store=self._store()
        ).first()
        if not loc:
            raise DRFValidationError({'storage_location': 'Unknown storage location.'})
        return loc

    def _branch(self, data, key, required=True):
        bid = data.get(key)
        if not bid and not required:
            # default to the main branch (or any branch) for the store
            b = (Branch.objects.filter(store=self._store(), is_main_branch=True).first()
                 or Branch.objects.filter(store=self._store()).first())
            if not b:
                raise DRFValidationError({key: 'Store has no branch configured.'})
            return b
        b = Branch.objects.filter(id=bid, store=self._store()).first()
        if not b:
            raise DRFValidationError({key: 'Unknown branch.'})
        return b

    def _run(self, fn, **kwargs):
        try:
            return fn(**kwargs)
        except DjangoValidationError as exc:
            raise DRFValidationError({'detail': exc.messages})


class MoveToStorageView(_StorageOpsBase):
    """POST → park active stock into storage. MANAGER+."""
    def post(self, request):
        d = request.data
        variant  = self._variant(d)
        location = self._location(d)
        branch   = self._branch(d, 'from_branch')
        movement = self._run(
            storage_service.move_to_storage,
            variant=variant, qty=d.get('quantity'), from_branch=branch,
            storage_location=location, user=request.user,
            reason=d.get('reason'), note=d.get('note'),
        )
        log_activity(
            request=request,
            action=f"Moved {movement.quantity} {variant.sku} → storage ({location.name})",
            op_type=ActivityLog.OperationType.ADJUSTMENT,
            details={'movement_id': str(movement.id), 'sku': variant.sku,
                     'qty': str(movement.quantity), 'location': location.name},
        )
        return Response(StorageMovementSerializer(movement).data,
                        status=status.HTTP_201_CREATED)


class RetrieveFromStorageView(_StorageOpsBase):
    """POST → bring stock back from storage to a branch. MANAGER+."""
    def post(self, request):
        d = request.data
        variant  = self._variant(d)
        location = self._location(d)
        branch   = self._branch(d, 'to_branch')
        movement = self._run(
            storage_service.retrieve_from_storage,
            variant=variant, qty=d.get('quantity'), storage_location=location,
            to_branch=branch, user=request.user,
            reason=d.get('reason'), note=d.get('note'),
        )
        log_activity(
            request=request,
            action=f"Retrieved {movement.quantity} {variant.sku} from storage ({location.name})",
            op_type=ActivityLog.OperationType.ADJUSTMENT,
            details={'movement_id': str(movement.id), 'sku': variant.sku,
                     'qty': str(movement.quantity), 'location': location.name},
        )
        return Response(StorageMovementSerializer(movement).data,
                        status=status.HTTP_201_CREATED)


class WriteOffStorageView(_StorageOpsBase):
    """POST → write off (damage/dispose) stock in storage. ADMIN+."""
    permission_classes = [IsAuthenticated, IsAdminOrAbove]

    def post(self, request):
        d = request.data
        variant  = self._variant(d)
        location = self._location(d)
        branch   = self._branch(d, 'branch', required=False)
        movement = self._run(
            storage_service.write_off_from_storage,
            variant=variant, qty=d.get('quantity'), storage_location=location,
            branch=branch, reason=d.get('reason') or 'Storage write-off',
            user=request.user, note=d.get('note'),
        )
        log_activity(
            request=request,
            action=f"Wrote off {movement.quantity} {variant.sku} from storage ({location.name})",
            op_type=ActivityLog.OperationType.ADJUSTMENT,
            details={'movement_id': str(movement.id), 'sku': variant.sku,
                     'qty': str(movement.quantity), 'location': location.name,
                     'adjustment_id': str(movement.related_adjustment_id)},
        )
        return Response(StorageMovementSerializer(movement).data,
                        status=status.HTTP_201_CREATED)


class StorageMovementsView(APIView):
    """GET → storage movement log, newest first. Filters: storage_location,
    variant, direction. MANAGER+."""
    permission_classes = [IsAuthenticated, IsManagerOrAbove]

    def get(self, request):
        qs = (StorageMovement.objects
              .filter(store=request.user.store)
              .select_related('variant__product', 'storage_location',
                              'from_branch', 'to_branch', 'created_by'))
        loc = request.query_params.get('storage_location')
        if loc:
            qs = qs.filter(storage_location_id=loc)
        variant = request.query_params.get('variant')
        if variant:
            qs = qs.filter(variant_id=variant)
        direction = request.query_params.get('direction')
        if direction:
            qs = qs.filter(direction=direction)
        return Response(StorageMovementSerializer(qs, many=True).data)
