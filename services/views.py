from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.activity import log_activity
from core.models import ActivityLog
from users.permissions import RoleScopedPermission
from .desk import add_job_lines, labour_variant, quote, refund_lines
from .models import Service
from .serializers import ServiceSerializer, ServiceNoteSerializer


class _Refuse(Exception):
    """Leave the atomic block (rolling everything back) and answer with this response."""
    def __init__(self, detail=None, code=status.HTTP_400_BAD_REQUEST, response=None):
        super().__init__(detail)
        self.response = response if response is not None else Response({'detail': detail}, status=code)


def _till_checkout(request, invoice):
    """Complete a DRAFT sale with the till's own checkout action — the ONE place the sale policies live
    (payment plan, Ajel, credit limit, quota, oversell, expiry, store credit). Same caller, same body
    (`method` / `payments`), so a finished job is paid exactly like a till sale."""
    from finance.views import SalesInvoiceViewSet
    pk = str(invoice.pk)
    view = SalesInvoiceViewSet(request=request, args=(), kwargs={'pk': pk},
                               action='checkout', format_kwarg=None)
    return view.checkout(request, pk=pk)


class ServiceViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list':               'CASHIER',
        'retrieve':           'CASHIER',
        'create':             'CASHIER',
        'update':             'MANAGER',
        'partial_update':     'MANAGER',
        'destroy':            'MANAGER',
        'done':               'CASHIER',
        'cancel':             'MANAGER',
        'archive':            'MANAGER',
        'toggle_bell':        'CASHIER',
        # A return gives money back — same minimum role as a normal return (finance RefundInvoice create).
        'return_service':     'MANAGER',
        'notes':              'CASHIER',
        'technicians':        'CASHIER',
        'quote':              'CASHIER',
        'labour':             'MANAGER',
        'run_notifications':  'MANAGER',
    }
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['receive_date', 'eta_datetime', 'created_at', 'cost', 'serial_number']

    def get_queryset(self):
        store = self.request.user.store
        qs = (Service.objects.filter(store=store)
              .select_related('client', 'invoice', 'refund', 'technician')
              .prefetch_related('parts__variant__product', 'notes__user'))

        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        technician = self.request.query_params.get('technician')
        if technician:
            if technician == 'none':
                qs = qs.filter(technician__isnull=True)
            else:
                try:
                    qs = qs.filter(technician_id=int(technician))
                except (TypeError, ValueError):
                    qs = qs.none()

        search = self.request.query_params.get('search', '').strip()
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(serial_number__icontains=search) |
                Q(client__name__icontains=search) |
                Q(client_name__icontains=search) |
                Q(client_phone__icontains=search) |
                Q(client__phone_number__icontains=search) |
                Q(service_type__icontains=search) |
                Q(device__icontains=search) |
                Q(device_serial__icontains=search)
            )

        return qs

    def perform_create(self, serializer):
        serializer.save(
            store=self.request.user.store,
            created_by=self.request.user,
        )

    def perform_update(self, serializer):
        instance = self.get_object()
        old_cost      = instance.cost
        old_diagnosis = instance.diagnosis
        updated = serializer.save()

        new_cost      = updated.cost
        new_diagnosis = updated.diagnosis

        cost_changed      = Decimal(str(new_cost)) != Decimal(str(old_cost))
        diagnosis_changed = new_diagnosis.strip() != old_diagnosis.strip()

        if cost_changed or diagnosis_changed:
            self._fire_update_notification(updated, cost_changed, diagnosis_changed)

    def _fire_update_notification(self, service, cost_changed, diagnosis_changed):
        from notifications.models import Notification

        store = service.store
        serial = service.serial_number

        from notifications.dispatcher import send_notification, msg
        if diagnosis_changed and cost_changed:
            title = f"Service {serial} — diagnosis & cost updated"
            body  = f"Diagnosis added and cost changed to {service.cost}."
            i18n  = msg('service_both.title', 'service_both.body', serial=serial, cost=service.cost)
        elif diagnosis_changed:
            title = f"Service {serial} — diagnosis updated"
            body  = service.diagnosis[:200] if service.diagnosis else ''
            i18n  = msg('service_diagnosis.title', serial=serial)   # body = the diagnosis itself (free text)
        else:
            title = f"Service {serial} — cost updated"
            body  = f"New cost: {service.cost}"
            i18n  = msg('service_cost.title', 'service_cost.body', serial=serial, cost=service.cost)

        # ONE store-wide notification (user=None = every member sees it). The old per-staff loop used a
        # field that doesn't exist (`recipient_id`) and crashed the request AFTER the save (s156, §AUDIT A7).
        send_notification(store=store, title=title, body=body,
                          priority=Notification.Priority.WARNING, link='/services',
                          i18n=i18n)   # s167

    @action(detail=True, methods=['post'])
    def done(self, request, pk=None):
        """Finish the job and bill it (Upgrade 13): a real sale with a labour line (detection fee + repair
        charge) and one line per part, taxed like any sale, completed by the till's own checkout — so payment
        (`method`, or split `payments`), Ajel-needs-a-named-customer, the credit policy, the invoice quota, the
        oversell check and the parts' stock-out are exactly the till's. Anything refused → nothing is kept."""
        service = self.get_object()
        store = request.user.store
        try:
            with transaction.atomic():
                # Lock the job: two "Done" clicks at once must not make two invoices.
                service = Service.objects.select_for_update().get(pk=service.pk, store=store)
                if service.status == Service.Status.DONE or service.invoice_id:
                    raise _Refuse('Service is already marked as Done.')
                if service.status == Service.Status.CANCELLED:
                    raise _Refuse('Cannot mark a cancelled service as Done.')
                if service.status != Service.Status.OPEN:
                    raise _Refuse('Only an open job can be marked Done.')

                from finance.models import SalesInvoice
                from finance.serializers import SalesInvoiceSerializer
                from users.models import Customer
                from core.models import Branch

                # Resolve customer (registered client or walk-in)
                customer = service.client
                if not customer:
                    customer = Customer.objects.filter(store=store, is_walk_in=True).first()
                if not customer:
                    raise _Refuse('No walk-in customer found for this store.', status.HTTP_500_INTERNAL_SERVER_ERROR)

                # Resolve branch (user default or store primary)
                branch = getattr(request.user, 'default_branch', None)
                if branch is not None and branch.store_id != store.id:
                    branch = None
                if not branch:
                    branch = Branch.objects.filter(store=store).first()
                if not branch:
                    raise _Refuse('No branch found for this store.', status.HTTP_500_INTERNAL_SERVER_ERROR)

                invoice = SalesInvoice.objects.create(
                    store=store, branch=branch, customer=customer,
                    status=SalesInvoice.Status.DRAFT, date=timezone.now(),
                )
                add_job_lines(service, invoice)
                SalesInvoiceSerializer._recalculate(invoice)   # the normal sale tax rule (per-line snapshot)

                # How the customer paid (Yakot 2026-10-06, option a — like the till; §AUDIT A8): a charged job
                # must say how. The till's checkout alone would post it unpaid.
                if invoice.grand_total > 0 and not (request.data.get('method') or request.data.get('payments')):
                    raise _Refuse('Choose how the customer paid.')
                # The Done window sends the total it showed (from /quote/). If the job changed since, stop —
                # the cashier must not collect one amount while the invoice records another.
                shown = request.data.get('expected_total')
                if shown not in (None, ''):
                    try:
                        shown = Decimal(str(shown))
                    except (InvalidOperation, ValueError):
                        raise _Refuse('Unreadable total.')
                    if shown != invoice.grand_total:
                        raise _Refuse(f'The job total changed to {invoice.grand_total:.2f} — reopen it and check again.',
                                      status.HTTP_409_CONFLICT)

                result = _till_checkout(request, invoice)
                if result.status_code >= 400:
                    raise _Refuse(response=result)

                service.status = Service.Status.DONE
                service.invoice = invoice
                service.save()
        except _Refuse as r:
            return r.response

        service = self.get_queryset().get(pk=service.pk)
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

    @action(detail=True, methods=['post'], url_path='return')
    def return_service(self, request, pk=None):
        """The customer brings the job back (Upgrade 13). If it was billed, everything still unreturned on its
        sale goes back through the NORMAL return (finance RefundInvoice): money back only up to what was paid
        (an Ajel job just cancels the debt), the drawer and store credit kept right, the shop's restocking-fee
        policy applied, parts back on the shelf (`restock_parts`: false for a dead part). Body (all optional):
        refund_method CASH | STORE_CREDIT, restock_parts, reason."""
        service = self.get_object()
        store = request.user.store
        from finance.models import SalesInvoice
        from finance.serializers import RefundInvoiceSerializer

        with transaction.atomic():
            service = Service.objects.select_for_update().get(pk=service.pk, store=store)
            if service.status != Service.Status.DONE:
                return Response(
                    {'detail': 'Only completed (Done) services can be returned.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            refund = None
            invoice = service.invoice
            if invoice is not None and invoice.status == SalesInvoice.Status.POSTED:
                restock = request.data.get('restock_parts', True)
                restock = not (restock is False or str(restock).lower() in ('false', '0', 'no'))
                lines = refund_lines(invoice, restock_parts=restock)
                if lines:
                    ser = RefundInvoiceSerializer(data={
                        'branch': invoice.branch_id,
                        'original_invoice': invoice.id,
                        'customer': invoice.customer_id,
                        'refund_method': request.data.get('refund_method') or 'CASH',
                        'reason': (request.data.get('reason') or '').strip()
                                  or f'Service job {service.serial_number} returned',
                        'items': lines,
                    }, context={'request': request})
                    ser.is_valid(raise_exception=True)
                    refund = ser.save(store=store, created_by=request.user)
                    log_activity(
                        request=request,
                        action=f"Created Return #{refund.refund_number or refund.id} (service {service.serial_number})",
                        op_type=ActivityLog.OperationType.RETURN,
                        details={
                            'refund_id': str(refund.id),
                            'original_invoice': invoice.invoice_number,
                            'service': service.serial_number,
                            'total': str(refund.total_refunded),
                        },
                    )

            service.status = Service.Status.RETURNED
            service.refund = refund
            service.save(update_fields=['status', 'refund', 'updated_at'])

        service = self.get_queryset().get(pk=service.pk)
        return Response(ServiceSerializer(service, context={'request': request}).data)

    @action(detail=True, methods=['get'])
    def quote(self, request, pk=None):
        """What "Done" will bill, to the cent: the lines, the tax and the grand total, computed with the normal
        sale rule and saved nowhere. The Done window and the ticket show THIS total, so the cashier collects
        exactly what the invoice will say (a single payment method pays the whole grand total)."""
        return Response(quote(self.get_object()))

    @action(detail=False, methods=['get', 'patch'])
    def labour(self, request):
        """The shop's system "Repair service" product that labour lines point at: its name + its own tax
        (`tax`: a tax id of this shop, or null = the shop's default tax). PATCH renames / re-taxes it."""
        from inventory.models import Tax
        store = request.user.store
        with transaction.atomic():
            variant = labour_variant(store)
            product = variant.product
            if request.method == 'PATCH':
                fields = []
                if 'name' in request.data:
                    name = (request.data.get('name') or '').strip()
                    if not name:
                        return Response({'detail': 'Give the repair line a name.'}, status=status.HTTP_400_BAD_REQUEST)
                    product.name = name[:255]
                    fields.append('name')
                if 'tax' in request.data:
                    tax_id = request.data.get('tax')
                    tax = None
                    if tax_id:
                        try:
                            tax = Tax.objects.filter(store=store, is_deleted=False, pk=tax_id).first()
                        except (ValueError, DjangoValidationError):
                            tax = None
                        if tax is None:
                            return Response({'detail': 'Unknown tax.'}, status=status.HTTP_400_BAD_REQUEST)
                    product.tax = tax
                    fields.append('tax')
                if fields:
                    product.save(update_fields=fields + ['updated_at'])
        return Response({'product': str(product.id), 'variant': str(variant.id),
                         'name': product.name, 'tax': str(product.tax_id) if product.tax_id else None})

    @action(detail=False, methods=['get'])
    def technicians(self, request):
        """Who can be put on a job: the active users of the caller's shop (id + name only — the staff list
        itself is manager-only, but the cashier who books a job in must be able to pick a technician)."""
        from users.models import User
        users = User.objects.filter(store=request.user.store, is_active=True).order_by('first_name', 'username')
        return Response([{'id': u.id, 'name': (u.get_full_name() or u.username).strip()} for u in users])

    @action(detail=True, methods=['post'])
    def notes(self, request, pk=None):
        """Add one dated note to the job's timeline (Proline tb_Ser_Service_Tasks)."""
        service = self.get_object()
        ser = ServiceNoteSerializer(data=request.data, context={'request': request})
        ser.is_valid(raise_exception=True)
        ser.save(store=service.store, service=service, user=request.user)
        return Response(ser.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='run-notifications')
    def run_notifications(self, request):
        call_command('check_service_notifications')
        return Response({'status': 'Notifications check completed.'}, status=status.HTTP_200_OK)
