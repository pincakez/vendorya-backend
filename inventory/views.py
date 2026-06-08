from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.utils import timezone
from django.db.models import Q, Sum, F, Min, Value, OuterRef, Subquery, ExpressionWrapper, DecimalField
from django.db.models.functions import Coalesce
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from users.permissions import RoleScopedPermission, IsManagerOrAbove, IsAdminOrAbove
from .models import Product, Category, Supplier, AttributeDefinition, ProductVariant, Tax, StockAdjustment, StockTransfer
from .serializers import (
    ProductListSerializer, ProductDetailSerializer, ProductWriteSerializer,
    ProductVariantSerializer,
    CategorySerializer, SupplierSerializer, AttributeDefinitionSerializer, TaxSerializer,
    StockAdjustmentSerializer, StockTransferSerializer,
)
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


class ProductViewSet(viewsets.ModelViewSet):
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
    }
    filter_backends = [filters.SearchFilter, VisibilityOrderingFilter]
    fv_table_id = 'inventory_products'
    search_fields = ['name', 'variants__sku', 'variants__barcode']
    # Server-side sort. FE maps column keys -> these (see Products.vue ORDER_MAP).
    ordering_fields = ['name', 'supplier__name', 'created_at',
                       'o_sku', 'o_wholesale', 'o_retail', 'o_profit', 'o_stock']

    # Reserved params that are NOT dynamic attribute filters.
    _RESERVED_PARAMS = {'search', 'ordering', 'page', 'page_size', 'category'}

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return ProductWriteSerializer
        if self.action == 'retrieve':
            return ProductDetailSerializer
        return ProductListSerializer

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
        )

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

    @action(detail=True, methods=['post'], url_path='upload-image', parser_classes=None)
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

    @action(detail=True, methods=['post'])
    def toggle_ghost(self, request, pk=None):
        """Flip a single product's POS visibility (ghost / un-ghost)."""
        product = self.get_object()
        product.hide_from_pos = not product.hide_from_pos
        product.save(update_fields=['hide_from_pos', 'updated_at'])
        log_activity(request=request, op_type=ActivityLog.OperationType.OTHER,
                     action=f"{'Ghosted' if product.hide_from_pos else 'Un-ghosted'} product '{product.name}'")
        return Response({'id': str(product.id), 'hide_from_pos': product.hide_from_pos})

    @action(detail=False, methods=['post'])
    def bulk_ghost(self, request):
        """Set POS visibility on many products at once. Body: {ids, hide}."""
        qs = self._store_products(request.data.get('ids'))
        if qs is None:
            return Response({'error': 'ids must be a non-empty list.'}, status=status.HTTP_400_BAD_REQUEST)
        hide = bool(request.data.get('hide', True))
        with transaction.atomic():
            count = qs.update(hide_from_pos=hide)
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


class ProductVariantViewSet(viewsets.ModelViewSet):
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
    }

    def get_queryset(self):
        return Category.objects.filter(store=self.request.user.store)

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
            Product.objects.filter(category_id__in=[category.id] + desc_ids).update(
                is_deleted=True, deleted_at=now,
                delete_reason=reason, delete_note=note, deleted_by=request.user,
            )
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
    }
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'contact_info']
    ordering_fields = ['name', 'contact_info', 'balance', 'created_at']
    ordering = ['name']

    def get_queryset(self):
        outstanding = ExpressionWrapper(
            F('purchases__total_amount') - F('purchases__paid_amount'),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
        return (
            Supplier.objects
            .filter(store=self.request.user.store)
            .annotate(balance=Coalesce(
                Sum(outstanding, filter=Q(purchases__is_deleted=False)),
                Decimal('0'),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ))
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


class SupplierPrefixCheckView(APIView):
    """GET /api/inventory/suppliers/check-prefix/?prefix=101 — store-scoped availability check."""
    permission_classes = [IsAuthenticated, IsManagerOrAbove]

    def get(self, request):
        prefix = request.query_params.get('prefix', '').strip()
        if not prefix or not prefix.isdigit() or len(prefix) != 3:
            return Response({'detail': 'Provide a 3-digit prefix.'}, status=status.HTTP_400_BAD_REQUEST)
        taken = Supplier.objects.filter(store=request.user.store, code_prefix=prefix).exists()
        return Response({'prefix': prefix, 'available': not taken})


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
        log_activity(
            request=request,
            action=f"Imported {result['summary'].get('created', 0)} products from CSV",
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
