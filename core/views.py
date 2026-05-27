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
