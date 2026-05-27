from django.utils import timezone
from django.db.models import Sum, Count
from rest_framework import viewsets, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Branch, ActivityLog
from .serializers import StoreSerializer, BranchSerializer, StoreSettingsSerializer, ActivityLogSerializer

_NO_STORE = Response({'detail': 'User has no store assigned.'}, status=status.HTTP_403_FORBIDDEN)


class StoreView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.store:
            return _NO_STORE
        return Response(StoreSerializer(request.user.store).data)

    def patch(self, request):
        if not request.user.store:
            return _NO_STORE
        serializer = StoreSerializer(request.user.store, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class StoreSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.store:
            return _NO_STORE
        return Response(StoreSettingsSerializer(request.user.store.settings).data)

    def patch(self, request):
        if not request.user.store:
            return _NO_STORE
        serializer = StoreSettingsSerializer(request.user.store.settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class BranchViewSet(viewsets.ModelViewSet):
    serializer_class = BranchSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        return Branch.objects.filter(store=self.request.user.store).select_related('address')

    def perform_create(self, serializer):
        serializer.save(store=self.request.user.store)


class ActivityLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ActivityLogSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ActivityLog.objects.filter(store=self.request.user.store).select_related('user')


LOW_STOCK_THRESHOLD = 5


class DashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        store = request.user.store
        if not store:
            return Response({'detail': 'User has no store assigned.'}, status=status.HTTP_403_FORBIDDEN)

        from finance.models import SalesInvoice, WorkShift
        from inventory.models import StockLevel

        today = timezone.localdate()

        # Today's posted sales
        today_invoices = SalesInvoice.objects.filter(
            store=store,
            status=SalesInvoice.Status.POSTED,
            date__date=today,
            is_deleted=False,
        )
        today_agg = today_invoices.aggregate(
            total=Sum('grand_total'),
            count=Count('id'),
        )
        items_sold = today_invoices.aggregate(
            qty=Sum('items__quantity')
        )['qty'] or 0

        # Open shift
        open_shift_qs = WorkShift.objects.filter(store=store, status=WorkShift.Status.OPEN).first()
        open_shift = None
        if open_shift_qs:
            open_shift = {
                'id': str(open_shift_qs.id),
                'start_time': open_shift_qs.start_time,
                'starting_cash': str(open_shift_qs.starting_cash),
                'user': open_shift_qs.user.get_full_name() or open_shift_qs.user.username,
            }

        # Low stock
        low_stock = (
            StockLevel.objects
            .filter(
                branch__store=store,
                quantity__lte=LOW_STOCK_THRESHOLD,
                variant__is_deleted=False,
                variant__product__is_deleted=False,
            )
            .select_related('variant__product', 'branch')
            .order_by('quantity')[:10]
        )
        low_stock_data = [
            {
                'sku': sl.variant.sku,
                'product_name': sl.variant.product.name,
                'branch': sl.branch.name,
                'quantity': str(sl.quantity),
            }
            for sl in low_stock
        ]

        # Recent sales (last 8 posted)
        recent_sales_qs = (
            SalesInvoice.objects
            .filter(store=store, status=SalesInvoice.Status.POSTED, is_deleted=False)
            .select_related('customer')
            .order_by('-date')[:8]
        )
        recent_sales = [
            {
                'id': str(inv.id),
                'invoice_number': inv.invoice_number,
                'customer': inv.customer.name,
                'grand_total': str(inv.grand_total),
                'date': inv.date,
            }
            for inv in recent_sales_qs
        ]

        return Response({
            'today_sales_total': str(today_agg['total'] or 0),
            'today_invoices_count': today_agg['count'] or 0,
            'today_items_sold': float(items_sold),
            'open_shift': open_shift,
            'low_stock_count': len(low_stock_data),
            'low_stock_items': low_stock_data,
            'recent_sales': recent_sales,
        })
