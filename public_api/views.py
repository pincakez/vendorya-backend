from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import RoleScopedPermission
from .models import APIKey
from .serializers import APIKeySerializer, APIKeyCreateSerializer
from .scopes import RESOURCE_GROUPS, ACCESS_LEVELS, all_scopes


class APIKeyViewSet(viewsets.ModelViewSet):
    """Manage a store's API keys. OWNER+ (a store owns its own keys).

    The raw key is returned ONLY in the create response. Listing shows the
    public prefix + metadata, never the secret. Queryset is tenant-scoped via
    APIKey.objects, so a store only ever sees its own keys.
    """
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list': 'OWNER', 'retrieve': 'OWNER', 'create': 'OWNER',
        'destroy': 'OWNER', 'revoke': 'OWNER',
    }
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        return APIKey.objects.filter(store=self.request.user.store)

    def get_serializer_class(self):
        return APIKeyCreateSerializer if self.action == 'create' else APIKeySerializer

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        obj = ser.save()
        data = APIKeySerializer(obj).data
        # One-time secret — show it now; it's unrecoverable afterwards.
        data['raw_key'] = obj._raw_key
        data['warning'] = 'Copy this key now — it will never be shown again.'
        return Response(data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        # Soft "revoke" on delete so request history/last-used is retained.
        instance.revoke()

    @action(detail=True, methods=['post'])
    def revoke(self, request, pk=None):
        key = self.get_object()
        key.revoke()
        return Response(APIKeySerializer(key).data)


class APIScopeCatalogView(APIView):
    """GET the grantable scope catalog — feeds the scope builder UI."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            'resource_groups': [
                {'group': g, 'label': label, 'levels': list(ACCESS_LEVELS)}
                for g, label in RESOURCE_GROUPS.items()
            ],
            'all_scopes': all_scopes(),
        })
