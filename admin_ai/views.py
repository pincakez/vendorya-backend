"""Admin AI HTTP layer.

All endpoints are sudo-only — gated by `IsSuperAdmin`.

Routes (mounted under /api/admin/ai/):
    GET/PATCH  /settings/                 -> AISettings singleton
    GET        /status/                   -> ping Gemini + return {status, has_key}
    GET/POST   /profiles/                 -> AIProfile CRUD
    POST       /profiles/{id}/activate/   -> set as the active profile
    GET        /models/                   -> AIModelCache list
    POST       /models/refresh/           -> pull from Gemini, return diff
    GET        /conversations/            -> sudo's own past conversations
    GET        /conversations/{id}/       -> one conversation with messages
    DELETE     /conversations/{id}/       -> soft-delete
    POST       /chat/                     -> SSE stream of a single user turn
    POST       /kb/                       -> upsert a knowledge chunk (embeds it)
    GET        /kb/                       -> list chunks (sourced for KB tab)
    POST       /kb/search/                -> top-k semantic search
"""
import json
import logging
from typing import Any, Dict, List

from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsSuperAdmin

from .models import (
    AISettings, AIProfile, AIModelCache,
    AIConversation, AIMessage, AIKnowledgeChunk,
)
from .serializers import (
    AISettingsSerializer, AIProfileSerializer, AIModelCacheSerializer,
    AIConversationSerializer, AIMessageSerializer, AIKnowledgeChunkSerializer,
)
from .services import GeminiService, NoApiKey, GeminiError
from .registry import registry, ToolContext

logger = logging.getLogger(__name__)


def _gemini_or_error():
    """Returns a (service, error_response) tuple. Exactly one is None."""
    try:
        return GeminiService.from_settings(), None
    except NoApiKey:
        return None, Response(
            {'error': 'Gemini API key is not configured. Set it on the Misc settings page.'},
            status=status.HTTP_409_CONFLICT,
        )


# ---------- settings ----------

class AISettingsView(APIView):
    """Singleton get/patch for the platform Gemini key."""
    permission_classes = [IsSuperAdmin]

    def get(self, request):
        return Response(AISettingsSerializer(AISettings.load()).data)

    def patch(self, request):
        obj = AISettings.load()
        ser = AISettingsSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(AISettingsSerializer(obj).data)


# ---------- status ----------

class AIStatusView(APIView):
    """Footer indicator: 'Connected' / 'No Key' / 'Error'."""
    permission_classes = [IsSuperAdmin]

    def get(self, request):
        settings_row = AISettings.load()
        if not settings_row.has_key:
            return Response({'status': 'no_key', 'has_key': False})
        try:
            service = GeminiService(settings_row.gemini_api_key)
        except NoApiKey:
            return Response({'status': 'no_key', 'has_key': False})
        result = service.ping()
        return Response({
            'status': 'connected' if result.get('ok') else 'error',
            'has_key': True,
            'detail': result.get('error') or '',
        })


# ---------- profiles ----------

class AIProfileViewSet(viewsets.ModelViewSet):
    serializer_class = AIProfileSerializer
    permission_classes = [IsSuperAdmin]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        return AIProfile.objects.filter(is_deleted=False).order_by('-is_active', 'name')

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        profile = self.get_object()
        profile.is_active = True
        profile.save()  # model.save() flips all other profiles inactive
        return Response(AIProfileSerializer(profile).data)


# ---------- model catalog ----------

class AIModelCacheViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class   = AIModelCacheSerializer
    permission_classes = [IsSuperAdmin]

    def get_queryset(self):
        return AIModelCache.objects.all().order_by('model_id')

    @action(detail=False, methods=['post'])
    def refresh(self, request):
        service, err = _gemini_or_error()
        if err:
            return err
        try:
            diff = service.refresh_models()
        except GeminiError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(diff)


# ---------- conversations ----------

class AIConversationViewSet(viewsets.ModelViewSet):
    """Each sudo user only sees their own conversations."""
    serializer_class   = AIConversationSerializer
    permission_classes = [IsSuperAdmin]
    http_method_names  = ['get', 'delete', 'head', 'options']

    def get_queryset(self):
        return (AIConversation.objects
                .filter(is_deleted=False, user=self.request.user)
                .order_by('-updated_at'))

    def retrieve(self, request, *args, **kwargs):
        conv = self.get_object()
        return Response({
            **AIConversationSerializer(conv).data,
            'messages': AIMessageSerializer(conv.messages.all(), many=True).data,
        })


# ---------- chat (SSE) ----------

class AIChatView(APIView):
    """Stream a single turn back as Server-Sent Events.

    POST body:
        {
          'conversation_id': uuid | null,
          'prompt': '...',
          'attachments': [{'mime_type': '...', 'data': 'base64...'}],
        }

    The X-Store-ID header (already honored by VendoryaJWTAuthentication) is
    what auto-scopes tool calls to a specific store.
    """
    permission_classes = [IsSuperAdmin]

    def post(self, request):
        service, err = _gemini_or_error()
        if err:
            return err

        prompt: str = (request.data.get('prompt') or '').strip()
        if not prompt:
            return Response({'error': 'prompt is required.'}, status=status.HTTP_400_BAD_REQUEST)

        active_profile = AIProfile.objects.filter(is_active=True, is_deleted=False).first()
        if active_profile is None:
            return Response({'error': 'No active AI profile configured.'},
                            status=status.HTTP_409_CONFLICT)

        # Resolve / create the conversation.
        conv_id = request.data.get('conversation_id')
        if conv_id:
            conversation = get_object_or_404(
                AIConversation.objects.filter(is_deleted=False),
                pk=conv_id, user=request.user,
            )
        else:
            conversation = AIConversation.objects.create(
                user=request.user,
                acting_store=getattr(request.user, 'store', None),
                profile=active_profile,
                title=prompt[:120],
            )

        attachments = request.data.get('attachments') or []
        # Persist the user turn before we start streaming.
        AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.USER,
            content=prompt,
            attachments=[{'mime_type': a.get('mime_type'), 'kind': 'inline'}
                         for a in attachments],
        )

        history = self._flatten_history(conversation)
        tool_ctx = ToolContext(
            user=request.user,
            store=getattr(request.user, 'store', None),
            request=request,
        )

        def event_stream():
            # First frame: conversation id so the client can pin subsequent turns.
            yield self._sse('meta', {'conversation_id': str(conversation.id)})

            collected_text: List[str] = []
            tool_calls_log: List[Dict[str, Any]] = []
            final_usage: Dict[str, Any] = {}

            try:
                for ev in service.chat_stream(
                    profile=active_profile,
                    history=history,
                    prompt=prompt,
                    attachments=attachments,
                    tool_context=tool_ctx,
                ):
                    kind = ev.get('event')
                    if kind == 'token':
                        collected_text.append(ev.get('text', ''))
                    elif kind == 'tool':
                        tool_calls_log.append(ev)
                    elif kind == 'done':
                        final_usage = ev.get('usage') or {}
                    yield self._sse(kind, ev)
            except Exception as e:  # noqa: BLE001
                logger.exception("Chat stream failed")
                yield self._sse('error', {'message': str(e)})

            # Persist the model turn at the end so resumes show full text.
            AIMessage.objects.create(
                conversation=conversation,
                role=AIMessage.Role.MODEL,
                content=''.join(collected_text),
                tool_calls=tool_calls_log,
                usage=final_usage,
            )

        response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response

    @staticmethod
    def _sse(event: str, data: Dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

    @staticmethod
    def _flatten_history(conversation: AIConversation) -> List[Dict[str, Any]]:
        return [
            {'role': m.role, 'content': m.content}
            for m in conversation.messages.exclude(role=AIMessage.Role.SYSTEM)
        ]


# ---------- knowledge base ----------

class AIKnowledgeChunkViewSet(viewsets.ModelViewSet):
    """KB CRUD + semantic search. Embeddings are computed server-side on save."""
    serializer_class   = AIKnowledgeChunkSerializer
    permission_classes = [IsSuperAdmin]
    http_method_names  = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        return AIKnowledgeChunk.objects.filter(is_deleted=False).order_by('-created_at')

    def perform_create(self, serializer):
        service, err = _gemini_or_error()
        if err is not None:
            # Without a key we still want admins to be able to seed text — they
            # just can't search it semantically. Save with no embedding.
            serializer.save()
            return
        chunk = serializer.save()
        try:
            chunk.embedding = service.embed(chunk.content)
            chunk.save(update_fields=['embedding'])
        except GeminiError as e:
            logger.warning("Embedding failed for chunk %s: %s", chunk.id, e)

    @action(detail=False, methods=['post'])
    def search(self, request):
        query = (request.data.get('query') or '').strip()
        if not query:
            return Response({'error': 'query is required.'}, status=status.HTTP_400_BAD_REQUEST)
        limit = int(request.data.get('limit') or 5)
        industries = request.data.get('industries') or []

        service, err = _gemini_or_error()
        if err:
            return err
        try:
            vec = service.embed(query)
        except GeminiError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        from pgvector.django import CosineDistance
        qs = AIKnowledgeChunk.objects.filter(is_deleted=False, embedding__isnull=False)
        if industries:
            qs = qs.filter(industries__overlap=industries)
        qs = (qs
              .annotate(distance=CosineDistance('embedding', vec))
              .order_by('distance')[:limit])
        return Response([
            {
                **AIKnowledgeChunkSerializer(c).data,
                'distance': float(c.distance),
            }
            for c in qs
        ])


# ---------- tools (introspection) ----------

class AIToolListView(APIView):
    """List all registered tools — used by the C4 Functions tab."""
    permission_classes = [IsSuperAdmin]

    def get(self, request):
        return Response([
            {
                'name': s.name,
                'description': s.description,
                'write': s.write,
                'requires_store': s.requires_store,
                'parameters': s.parameters,
            }
            for s in registry.all()
        ])
