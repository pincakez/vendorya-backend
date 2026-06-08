from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from users.permissions import RoleScopedPermission
from users.models import User
from .models import TablePreset, TablePreference
from .serializers import TablePresetSerializer


class TablePresetViewSet(viewsets.ModelViewSet):
    """Layer 2 — store-owned named column layouts + per-user assignment.

    Authoring/assignment is ADMIN+ (Owner/Admin/sudo). `effective` is open to any
    authenticated user (they read their own resolved layout).
    """
    serializer_class = TablePresetSerializer
    permission_classes = [IsAuthenticated, RoleScopedPermission]
    role_map = {
        'list': 'ADMIN', 'retrieve': 'ADMIN', 'create': 'ADMIN',
        'update': 'ADMIN', 'partial_update': 'ADMIN', 'destroy': 'ADMIN',
        'effective': 'CASHIER', 'assignments': 'ADMIN', 'my_config': 'CASHIER',
    }

    def get_queryset(self):
        qs = TablePreset.objects.filter(store=self.request.user.store)
        tid = self.request.query_params.get('table_id')
        if tid:
            qs = qs.filter(table_id=tid)
        return qs.order_by('name')

    def perform_create(self, serializer):
        self._enforce_single_default(serializer.validated_data)
        serializer.save(store=self.request.user.store, created_by=self.request.user)

    def perform_update(self, serializer):
        self._enforce_single_default(serializer.validated_data)
        serializer.save()

    def _enforce_single_default(self, data):
        """Only one default preset per (store, table_id)."""
        if data.get('is_default'):
            tid = data.get('table_id') or self.get_object().table_id
            TablePreset.objects.filter(
                store=self.request.user.store, table_id=tid, is_default=True,
            ).update(is_default=False)

    @action(detail=False, methods=['get'])
    def effective(self, request):
        """Resolve ?table_id= for the current user: assigned preset, else store default, else {}."""
        tid = request.query_params.get('table_id')
        store = request.user.store
        preset = None
        pref = (TablePreference.objects
                .filter(user=request.user, table_id=tid)
                .select_related('assigned_preset').first())
        if pref and pref.assigned_preset_id:
            preset = pref.assigned_preset
        if preset is None and store is not None:
            preset = TablePreset.objects.filter(store=store, table_id=tid, is_default=True).first()
        return Response(TablePresetSerializer(preset).data if preset else {})

    @action(detail=False, methods=['get', 'post'])
    def assignments(self, request):
        """GET ?table_id= -> staff + their assigned preset; POST {user_id,table_id,preset_id} to assign."""
        store = request.user.store
        if request.method == 'GET':
            tid = request.query_params.get('table_id')
            users = User.objects.filter(store=store, is_active=True, is_superadmin=False).order_by('role', 'username')
            prefs = {p.user_id: p.assigned_preset_id
                     for p in TablePreference.objects.filter(store=store, table_id=tid)}
            return Response([{
                'user_id': str(u.id),
                'username': u.username,
                'full_name': f'{u.first_name} {u.last_name}'.strip() or u.username,
                'role': u.role,
                'preset_id': str(prefs[u.id]) if prefs.get(u.id) else None,
            } for u in users])

        uid, tid, pid = request.data.get('user_id'), request.data.get('table_id'), request.data.get('preset_id')
        target = User.objects.filter(id=uid, store=store).first()
        if not target:
            return Response({'detail': 'User not found.'}, status=404)
        preset = TablePreset.objects.filter(id=pid, store=store, table_id=tid).first() if pid else None
        pref, _ = TablePreference.objects.get_or_create(user=target, table_id=tid, defaults={'store': store})
        pref.store = store
        pref.assigned_preset = preset
        pref.save()
        return Response({'ok': True})

    @action(detail=False, methods=['get', 'post'], url_path='my-config')
    def my_config(self, request):
        """GET/POST ?table_id= — per-user ad-hoc layout saved server-side.
        Complements the browser localStorage copy so the layout survives clearing
        cache or switching devices."""
        tid = request.query_params.get('table_id') or request.data.get('table_id')
        store = request.user.store
        if request.method == 'GET':
            pref = TablePreference.objects.filter(user=request.user, table_id=tid).first()
            return Response(pref.config if pref and pref.config else {})
        cfg = request.data.get('config')
        if cfg is None:
            return Response({'detail': 'config is required.'}, status=400)
        pref, _ = TablePreference.objects.get_or_create(
            user=request.user, table_id=tid,
            defaults={'store': store}
        )
        pref.store = store
        pref.config = cfg
        pref.save(update_fields=['config'])
        return Response({'ok': True})
