from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from users.permissions import RoleScopedPermission
from .models import Service
from .serializers import ServiceSerializer


class ServiceViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':           'CASHIER',
        'retrieve':       'CASHIER',
        'create':         'CASHIER',
        'update':         'MANAGER',
        'partial_update': 'MANAGER',
        'destroy':        'MANAGER',
        'done':           'CASHIER',
        'cancel':         'MANAGER',
        'archive':        'MANAGER',
        'toggle_bell':    'CASHIER',
    }
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['receive_date', 'eta_datetime', 'created_at', 'cost', 'serial_number']

    def get_queryset(self):
        store = self.request.user.store
        qs = Service.objects.filter(store=store).select_related('client', 'invoice')

        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        search = self.request.query_params.get('search', '').strip()
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(serial_number__icontains=search) |
                Q(client__name__icontains=search) |
                Q(client_name__icontains=search) |
                Q(client_phone__icontains=search) |
                Q(client__phone_number__icontains=search) |
                Q(service_type__icontains=search)
            )

        return qs

    def perform_create(self, serializer):
        serializer.save(
            store=self.request.user.store,
            created_by=self.request.user,
        )

    @action(detail=True, methods=['post'])
    def done(self, request, pk=None):
        service = self.get_object()

        if service.status == Service.Status.DONE:
            return Response({'detail': 'Service is already marked as Done.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if service.status == Service.Status.CANCELLED:
            return Response({'detail': 'Cannot mark a cancelled service as Done.'},
                            status=status.HTTP_400_BAD_REQUEST)

        store = request.user.store

        with transaction.atomic():
            from finance.models import SalesInvoice
            from users.models import Customer
            from core.models import Branch

            # Resolve customer (registered client or walk-in)
            customer = service.client
            if not customer:
                customer = Customer.objects.filter(store=store, is_walk_in=True).first()
            if not customer:
                return Response({'detail': 'No walk-in customer found for this store.'},
                                status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # Resolve branch (user default or store primary)
            branch = getattr(request.user, 'default_branch', None)
            if not branch:
                branch = Branch.objects.filter(store=store).first()
            if not branch:
                return Response({'detail': 'No branch found for this store.'},
                                status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            cost = service.cost or Decimal('0.00')

            invoice = SalesInvoice.objects.create(
                store=store,
                branch=branch,
                customer=customer,
                status=SalesInvoice.Status.POSTED,
                date=timezone.now(),
                subtotal=cost,
                tax_total=Decimal('0.00'),
                discount=Decimal('0.00'),
                grand_total=cost,
                paid_amount=Decimal('0.00'),
            )

            service.status = Service.Status.DONE
            service.invoice = invoice
            service.save()

        return Response(ServiceSerializer(service, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        service = self.get_object()

        if service.status == Service.Status.DONE:
            return Response({'detail': 'Cannot cancel a completed service.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if service.status in (Service.Status.CANCELLED, Service.Status.ARCHIVED):
            return Response({'detail': f'Service is already {service.status.lower()}.'},
                            status=status.HTTP_400_BAD_REQUEST)

        service.status = Service.Status.CANCELLED
        service.save()
        return Response(ServiceSerializer(service, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        service = self.get_object()

        if service.status not in (Service.Status.DONE, Service.Status.CANCELLED):
            return Response(
                {'detail': 'Only Done or Cancelled services can be archived.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service.status = Service.Status.ARCHIVED
        service.save()
        return Response(ServiceSerializer(service, context={'request': request}).data)

    @action(detail=True, methods=['post'], url_path='toggle-bell')
    def toggle_bell(self, request, pk=None):
        service = self.get_object()
        service.notify_bell = not service.notify_bell
        service.save(update_fields=['notify_bell', 'updated_at'])
        return Response({'notify_bell': service.notify_bell})
