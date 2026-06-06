from datetime import date, timedelta
from django.db.models import Sum
from django.db.models.functions import Coalesce
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.models import Product
from inventory.serializers import ProductListSerializer
from users.permissions import RoleScopedPermission
from .models import POSFavoriteItem
from .serializers import POSFavoriteItemSerializer


class POSFavoriteItemViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    serializer_class = POSFavoriteItemSerializer
    role_map = {
        'list':    'CASHIER',
        'retrieve':'CASHIER',
        'create':  'MANAGER',
        'update':  'MANAGER',
        'partial_update': 'MANAGER',
        'destroy': 'MANAGER',
        'reorder': 'MANAGER',
    }

    def get_queryset(self):
        return POSFavoriteItem.objects.filter(store=self.request.user.store).select_related('product')

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)

    @action(detail=False, methods=['patch'])
    def reorder(self, request):
        """PATCH /api/pos/favorites/reorder/ with [{id, order}, ...] to update display order."""
        items = request.data
        if not isinstance(items, list):
            return Response({'detail': 'Expected a list of {id, order} objects.'}, status=400)
        store = request.user.store
        for item in items:
            POSFavoriteItem.objects.filter(id=item.get('id'), store=store).update(order=item.get('order', 0))
        return Response({'status': 'reordered'})


class TopSellingView(APIView):
    """GET /api/pos/top-selling/?period=month&category=<uuid>&limit=8"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from finance.models import SalesInvoiceItem

        store = request.user.store
        settings = getattr(store, 'settings', None)

        period   = request.query_params.get('period',   getattr(settings, 'pos_top_selling_period', 'month'))
        category = request.query_params.get('category', None)
        try:
            limit = int(request.query_params.get('limit', getattr(settings, 'pos_top_selling_limit', 8)))
        except (ValueError, TypeError):
            limit = 8

        today = date.today()
        if period == 'today':
            since = today
        elif period == 'week':
            since = today - timedelta(days=7)
        elif period == 'month':
            since = today.replace(day=1)
        else:
            since = None

        sold_qs = SalesInvoiceItem.objects.filter(
            invoice__store=store,
            invoice__status='POSTED',
        )
        if since:
            sold_qs = sold_qs.filter(invoice__date__date__gte=since)

        # Sum qty sold per variant → group by product
        from django.db.models import IntegerField
        top_variant_ids = (
            sold_qs
            .values('variant')
            .annotate(total_sold=Coalesce(Sum('quantity'), 0, output_field=IntegerField()))
            .order_by('-total_sold')
        )

        # Map variant → product, deduplicate
        seen_products = set()
        product_ids_ordered = []
        for row in top_variant_ids:
            from inventory.models import ProductVariant
            try:
                pv = ProductVariant.objects.select_related('product').get(id=row['variant'])
                pid = pv.product_id
                if pid not in seen_products:
                    seen_products.add(pid)
                    product_ids_ordered.append(pid)
                    if len(product_ids_ordered) >= limit:
                        break
            except ProductVariant.DoesNotExist:
                continue

        qs = Product.objects.filter(
            id__in=product_ids_ordered, store=store, is_deleted=False, hide_from_pos=False,
        )
        if category:
            qs = qs.filter(category_id=category)

        # Preserve ranking order
        products_by_id = {p.id: p for p in qs}
        ordered = [products_by_id[pid] for pid in product_ids_ordered if pid in products_by_id]

        serializer = ProductListSerializer(ordered, many=True, context={'request': request})
        return Response(serializer.data)
