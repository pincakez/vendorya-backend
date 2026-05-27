from django.db.models import Q
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Notification
from .serializers import NotificationSerializer


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """User-facing inbox.

    Returns notifications addressed directly to the user (`user=me`) plus
    store-wide notifications for the user's store (`user IS NULL`).
    Extras: `?unread=1`, POST `/{id}/read/`, POST `/read-all/`,
    GET `/unread-count/`.
    """
    serializer_class   = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        store = getattr(user, 'store', None)
        if not store:
            return Notification.objects.none()

        qs = Notification.objects.filter(
            Q(store=store) & (Q(user=user) | Q(user__isnull=True)),
        )
        if self.request.query_params.get('unread') in ('1', 'true', 'yes'):
            qs = qs.filter(read_at__isnull=True)
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
        qs = self.get_queryset().filter(read_at__isnull=True)
        count = qs.update(read_at=timezone.now())
        return Response({'updated': count})

    @action(detail=False, methods=['get'], url_path='unread-count')
    def unread_count(self, request):
        count = self.get_queryset().filter(read_at__isnull=True).count()
        return Response({'count': count})
