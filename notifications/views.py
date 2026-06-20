from django.db.models import Q
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsSuperAdmin
from .models import Notification, NotificationPreference, AdminSoundConfig, SOUND_CHOICES
from .serializers import NotificationSerializer, NotificationPreferenceSerializer
from .dispatcher import send_notification


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    User-facing inbox.
    Filters: ?unread=1  ?priority=INFO|WARNING|ALERT|ADMIN
    Actions: POST /{id}/read/  POST /read-all/  GET /unread-count/  GET /recent/
    """
    serializer_class   = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user  = self.request.user
        store = getattr(user, 'store', None)
        if not store:
            return Notification.objects.none()

        qs = Notification.objects.filter(
            Q(store=store) & (Q(user=user) | Q(user__isnull=True)),
        )
        if self.request.query_params.get('unread') in ('1', 'true', 'yes'):
            qs = qs.filter(read_at__isnull=True)
        priority = self.request.query_params.get('priority')
        if priority:
            qs = qs.filter(priority=priority.upper())
        return qs

    @action(detail=True, methods=['post'])
    def read(self, request, pk=None):
        n = self.get_object()
        if n.read_at is None:
            n.read_at = timezone.now()
            n.save(update_fields=['read_at'])
        return Response(NotificationSerializer(n).data)

    @action(detail=False, methods=['post'], url_path='read-all')
    def read_all(self, request):
        count = self.get_queryset().filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({'updated': count})

    @action(detail=False, methods=['get'], url_path='unread-count')
    def unread_count(self, request):
        count = self.get_queryset().filter(read_at__isnull=True).count()
        return Response({'count': count})

    @action(detail=False, methods=['get'], url_path='recent')
    def recent(self, request):
        """Last 5 unread — used by the bell dropdown."""
        items = self.get_queryset().filter(read_at__isnull=True)[:5]
        return Response(NotificationSerializer(items, many=True).data)


class NotificationPreferenceView(APIView):
    """GET / PUT  /api/notifications/preferences/

    Per-user sound prefs. A brand-new user's info/warning/alert sounds are seeded
    from the store's admin-set defaults. `admin_sound` is NOT user-editable — it is
    the platform-wide value from AdminSoundConfig (set only in the sudo area) and is
    injected into every response so the bell still knows what to play.
    """
    permission_classes = [IsAuthenticated]

    def _get_or_create(self, user):
        prefs, created = NotificationPreference.objects.get_or_create(user=user)
        if created:
            store_settings = getattr(getattr(user, 'store', None), 'settings', None)
            if store_settings:
                prefs.info_sound    = store_settings.default_info_sound    or prefs.info_sound
                prefs.warning_sound = store_settings.default_warning_sound or prefs.warning_sound
                prefs.alert_sound   = store_settings.default_alert_sound   or prefs.alert_sound
                prefs.save(update_fields=['info_sound', 'warning_sound', 'alert_sound'])
        return prefs

    def _with_admin_sound(self, data):
        data = dict(data)
        data['admin_sound'] = AdminSoundConfig.get_solo().admin_sound   # sudo-controlled
        return data

    def get(self, request):
        prefs = self._get_or_create(request.user)
        return Response(self._with_admin_sound(NotificationPreferenceSerializer(prefs).data))

    def put(self, request):
        prefs = self._get_or_create(request.user)
        # admin_sound is locked — drop it from the user payload before saving
        payload = {k: v for k, v in request.data.items() if k != 'admin_sound'}
        ser = NotificationPreferenceSerializer(prefs, data=payload, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(self._with_admin_sound(ser.data))


class AdminSoundConfigView(APIView):
    """GET / PUT  /api/notifications/admin-sound/ — sudo only.

    The platform-wide ADMIN-notification sound. Store users can't change it.
    """
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        return Response({'admin_sound': AdminSoundConfig.get_solo().admin_sound})

    def put(self, request):
        sound = request.data.get('admin_sound')
        valid = {c[0] for c in SOUND_CHOICES}
        if sound not in valid:
            return Response({'detail': f'Invalid sound id: {sound}'},
                            status=status.HTTP_400_BAD_REQUEST)
        cfg = AdminSoundConfig.get_solo()
        cfg.admin_sound = sound
        cfg.save(update_fields=['admin_sound'])
        return Response({'admin_sound': cfg.admin_sound})


class AdminAlertView(APIView):
    """
    POST /api/admin/alerts/send/
    Sudo-only. Sends an ADMIN-priority notification to one store, many stores, or ALL.

    Body:
      { "title": "...", "body": "...", "store_ids": ["uuid", ...] }
      or
      { "title": "...", "body": "...", "all_stores": true }
    """
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def post(self, request):
        from core.models import Store

        title = request.data.get('title', '').strip()
        body  = request.data.get('body', '').strip()
        if not title:
            return Response({'detail': 'title is required.'}, status=status.HTTP_400_BAD_REQUEST)

        all_stores = request.data.get('all_stores', False)
        if all_stores:
            stores = Store.objects.filter(is_active=True, is_deleted=False)
        else:
            ids = request.data.get('store_ids', [])
            if not ids:
                return Response({'detail': 'Provide store_ids or all_stores=true.'},
                                status=status.HTTP_400_BAD_REQUEST)
            stores = Store.objects.filter(id__in=ids, is_deleted=False)

        count = 0
        for store in stores:
            send_notification(
                store=store,
                title=title,
                body=body,
                priority=Notification.Priority.ADMIN,
                notif_type=Notification.Type.ADMIN_NOTE,
            )
            count += 1

        return Response({'sent_to': count})


class AdminAlertHistoryView(APIView):
    """
    GET /api/admin/alerts/history/?store_id=<uuid>&page=1
    Returns paginated ADMIN-priority notifications for a given store (sudo only).
    """
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        store_id = request.query_params.get('store_id')
        if not store_id:
            return Response({'detail': 'store_id required.'}, status=status.HTTP_400_BAD_REQUEST)

        qs = (
            Notification.all_objects   # sudo: reads any store's history (explicit store_id)
            .filter(store_id=store_id, priority=Notification.Priority.ADMIN)
            .order_by('-created_at')
        )

        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except ValueError:
            page = 1
        per_page = 20
        start = (page - 1) * per_page
        total = qs.count()
        items = qs[start:start + per_page]

        return Response({
            'count': total,
            'page': page,
            'pages': max(1, -(-total // per_page)),
            'results': NotificationSerializer(items, many=True).data,
        })
