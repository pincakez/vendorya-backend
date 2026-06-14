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
from typing import Any, Dict, List, Optional

from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.permissions import IsAuthenticated

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

# ---------------------------------------------------------------------------
#  Tool routing — send only the relevant tool group instead of all 48.
#  "Always on" tools orient the model (who am I, what store am I on).
#  Groups are activated by keyword match against the user's prompt.
# ---------------------------------------------------------------------------

_ALWAYS_ON = [
    'get_current_context', 'list_stores', 'get_store_info',
    'get_store_stats', 'get_activity_log', 'search_knowledge_base',
]

_TOOL_GROUPS: dict = {
    'inventory': [
        'list_branches', 'update_branch',
        'list_products', 'get_product_detail',
        'list_categories', 'create_category', 'update_category',
        'list_attributes', 'create_attribute', 'update_attribute',
        'bulk_update_attribute_value',
        'list_suppliers', 'get_supplier_detail', 'create_supplier', 'update_supplier',
        'list_stock_adjustments', 'create_stock_adjustment',
        'create_product', 'update_product',
    ],
    'finance': [
        'list_invoices', 'list_purchases', 'list_expenses',
        'create_purchase_invoice', 'receive_purchase',
        'create_sales_invoice', 'create_expense',
    ],
    'people': [
        'list_customers', 'get_customer_detail', 'create_customer', 'update_customer',
        'list_staff', 'create_staff_user', 'update_staff_user', 'deactivate_staff_user',
    ],
    'platform': [
        'list_admin_users', 'list_subscription_plans', 'list_subscriptions',
        'create_store', 'update_store', 'toggle_store_active',
        'update_subscription_plan', 'update_subscription', 'send_in_app_notification',
    ],
}

_ROUTING_KEYWORDS: dict = {
    'inventory': [
        'product', 'stock', 'inventory', 'item', 'sku', 'barcode', 'laptop', 'phone',
        'tablet', 'category', 'supplier', 'variant', 'adjustment', 'branch', 'attribute',
        'season', 'low stock', 'reorder', 'import', 'export', 'quantity', 'cost price',
        'sell price', 'how many', 'in stock',
    ],
    'finance': [
        'invoice', 'sale', 'purchase', 'expense', 'shift', 'payment', 'revenue',
        'profit', 'sell', 'sold', 'buy', 'paid', 'debt', 'receivable', 'payable',
        'income', 'receipt', 'bill', 'cash', 'total',
    ],
    'people': [
        'customer', 'staff', 'cashier', 'employee', 'user', 'client',
        'worker', 'team', 'member', 'who works',
    ],
    'platform': [
        'subscription', 'plan', 'billing', 'suspend', 'notification', 'notify',
        'alert', 'all stores', 'list stores', 'admin user',
    ],
}


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
        tool_names = self._route_tools(prompt)
        kb_context = self._retrieve_kb_context(service, prompt)

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
                    tool_names=tool_names,
                    kb_context=kb_context,
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
        """Return conversation history in Gemini's {role, parts} format.

        For model messages that contain tool calls, we reconstruct the
        function_call + function_response turn pairs Gemini expects. Without
        this the model loses knowledge of discovered IDs (store UUIDs etc.)
        between conversation turns, causing it to hallucinate or re-query.
        """
        contents: List[Dict[str, Any]] = []

        for m in conversation.messages.exclude(role=AIMessage.Role.SYSTEM):
            role = 'model' if m.role.upper() == 'MODEL' else 'user'

            if role == 'model' and m.tool_calls:
                tool_events = [tc for tc in m.tool_calls if tc.get('event') == 'tool']
                if tool_events:
                    # Reconstruct the model's function_call turn.
                    contents.append({
                        'role': 'model',
                        'parts': [
                            {'function_call': {'name': tc['name'], 'args': tc.get('args') or {}}}
                            for tc in tool_events
                        ],
                    })
                    # Reconstruct the user-side function_response turn.
                    contents.append({
                        'role': 'user',
                        'parts': [
                            {'function_response': {
                                'name': tc['name'],
                                'response': tc.get('result') or {},
                            }}
                            for tc in tool_events
                        ],
                    })
                # Then the model's final text answer for this turn.
                if m.content:
                    contents.append({'role': 'model', 'parts': [{'text': m.content}]})
            elif m.content:
                contents.append({'role': role, 'parts': [{'text': m.content}]})

        return contents

    @staticmethod
    def _route_tools(prompt: str) -> Optional[List[str]]:
        """Return a focused tool list based on the prompt's intent.

        Returns None when the intent is ambiguous — the caller then sends all
        registered tools (safe fallback). When a group matches, sends only the
        orientation tools + that group: typically ~10 tools vs 48 (~70% saving).
        """
        low = prompt.lower()
        matched: set = set()
        for group, keywords in _ROUTING_KEYWORDS.items():
            if any(kw in low for kw in keywords):
                matched.add(group)

        if not matched:
            return None  # unclear intent → send all tools

        names: List[str] = list(_ALWAYS_ON)
        for group in matched:
            names.extend(_TOOL_GROUPS[group])
        return names

    @staticmethod
    def _retrieve_kb_context(service, prompt: str, top_k: int = 4) -> Optional[str]:
        """Embed the prompt, find the most relevant KB chunks, return them as text.

        Returns None (silently) when KB is empty, embedding fails, or no chunk
        is close enough (distance ≥ 0.5 = less than 50% cosine similarity).
        This keeps the fast path free when the KB hasn't been populated yet.
        """
        try:
            from .models import AIKnowledgeChunk
            if not AIKnowledgeChunk.objects.filter(
                    is_deleted=False, embedding__isnull=False).exists():
                return None  # KB is empty — skip the embed call entirely

            from .services import GeminiError
            from pgvector.django import CosineDistance
            vec = service.embed(prompt)
            qs = (AIKnowledgeChunk.objects
                  .filter(is_deleted=False, embedding__isnull=False)
                  .annotate(distance=CosineDistance('embedding', vec))
                  .order_by('distance')[:top_k])
            relevant = [c for c in qs if c.distance < 0.5]
            if not relevant:
                return None
            parts = [f"[{c.source_name}]\n{c.content}" for c in relevant]
            return '\n\n'.join(parts)
        except Exception:  # noqa: BLE001 — never break the chat over a KB miss
            logger.warning("KB retrieval failed for prompt %r", prompt[:60], exc_info=True)
            return None


# ---------- knowledge base ----------

class AIKnowledgeChunkViewSet(viewsets.ModelViewSet):
    """KB CRUD + semantic search + file upload. Embeddings computed server-side."""
    serializer_class   = AIKnowledgeChunkSerializer
    permission_classes = [IsSuperAdmin]
    http_method_names  = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        return AIKnowledgeChunk.objects.filter(is_deleted=False).order_by('-created_at')

    def perform_create(self, serializer):
        service, err = _gemini_or_error()
        if err is not None:
            serializer.save()
            return
        chunk = serializer.save()
        try:
            chunk.embedding = service.embed(chunk.content)
            chunk.save(update_fields=['embedding'])
        except GeminiError as e:
            logger.warning("Embedding failed for chunk %s: %s", chunk.id, e)

    @action(detail=False, methods=['post'])
    def upload(self, request):
        """Parse a document and ingest all text chunks into the knowledge base.

        Accepts multipart/form-data with:
          file       — PDF / DOCX / CSV / TXT
          industries — JSON array or comma-separated string of tags
        """
        file = request.FILES.get('file')
        if not file:
            return Response({'error': 'file is required.'}, status=status.HTTP_400_BAD_REQUEST)

        MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB hard cap
        ALLOWED_EXTENSIONS = ('.txt', '.csv', '.pdf', '.docx')
        if file.size and file.size > MAX_UPLOAD_BYTES:
            return Response(
                {'error': f'File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).'},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        if not (file.name or '').lower().endswith(ALLOWED_EXTENSIONS):
            return Response(
                {'error': f'Unsupported file type. Allowed: {", ".join(ALLOWED_EXTENSIONS)}.'},
                status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)

        industries = request.data.get('industries') or []
        if isinstance(industries, str):
            try:
                industries = json.loads(industries)
            except (json.JSONDecodeError, ValueError):
                industries = [t.strip() for t in industries.split(',') if t.strip()]

        text = self._parse_file(file)
        if not text:
            return Response({'error': 'Could not extract text from the file.'},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        chunks = self._chunk_text(text)
        service, _ = _gemini_or_error()

        created_ids = []
        for idx, chunk_text in enumerate(chunks):
            chunk = AIKnowledgeChunk.objects.create(
                source_name=file.name,
                source_type='document',
                chunk_index=idx,
                content=chunk_text,
                industries=industries,
            )
            if service:
                try:
                    chunk.embedding = service.embed(chunk_text)
                    chunk.save(update_fields=['embedding'])
                except GeminiError as e:
                    logger.warning("Embedding skipped for chunk %s: %s", chunk.id, e)
            created_ids.append(str(chunk.id))

        return Response({'created': len(created_ids), 'ids': created_ids},
                        status=status.HTTP_201_CREATED)

    @staticmethod
    def _parse_file(file) -> str:
        name = (file.name or '').lower()
        raw = file.read()

        if name.endswith('.txt'):
            return raw.decode('utf-8', errors='ignore')

        if name.endswith('.csv'):
            import csv
            import io
            text = raw.decode('utf-8', errors='ignore')
            reader = csv.reader(io.StringIO(text))
            return '\n'.join(', '.join(row) for row in reader)

        if name.endswith('.pdf'):
            try:
                from pypdf import PdfReader
                import io
                reader = PdfReader(io.BytesIO(raw))
                pages = [p.extract_text() or '' for p in reader.pages]
                return '\n\n'.join(pages)
            except Exception as e:
                logger.warning("PDF parse failed: %s", e)
                return ''

        if name.endswith('.docx'):
            try:
                from docx import Document
                import io
                doc = Document(io.BytesIO(raw))
                return '\n'.join(p.text for p in doc.paragraphs if p.text.strip())
            except Exception as e:
                logger.warning("DOCX parse failed: %s", e)
                return ''

        # Fallback: try UTF-8 plain text.
        return raw.decode('utf-8', errors='ignore')

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 1200) -> list:
        paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
        if not paragraphs:
            return [text[:chunk_size]] if text.strip() else []

        chunks = []
        current: list = []
        current_len = 0

        for para in paragraphs:
            if current_len + len(para) > chunk_size and current:
                chunks.append('\n\n'.join(current))
                current = [para]
                current_len = len(para)
            else:
                current.append(para)
                current_len += len(para)

        if current:
            chunks.append('\n\n'.join(current))

        return chunks

    @action(detail=False, methods=['post'])
    def search(self, request):
        query = (request.data.get('query') or '').strip()
        if not query:
            return Response({'error': 'query is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            limit = int(request.data.get('limit') or 5)
        except (TypeError, ValueError):
            return Response({'error': 'limit must be an integer.'},
                            status=status.HTTP_400_BAD_REQUEST)
        limit = max(1, min(limit, 50))   # clamp to a sane range
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


# ---------- V-Agent: store-level AI insights ----------

class VAInsightsView(APIView):
    """Store-scoped AI insight widget. Reads only request.user.store — no cross-store access.

    GET /api/admin-ai/vagent/insights/
    Returns {"insights": [{type, title, body}, ...]} — 4 items, Egyptian Arabic.
    """
    permission_classes = [IsAuthenticated]

    PROFILE_NAME = 'V-Agent'

    SYSTEM_PROMPT = """أنت V-Agent، مساعد ذكاء اصطناعي متخصص في تحليل بيانات المتاجر وإعطاء نصائح تجارية عملية ومفيدة.

قواعد ثابتة:
- تكلم بالعربية المصرية الواضحة مباشرةً لصاحب المتجر
- كل نصيحة: عنوان قصير (5 كلمات أقصى) + 2-3 جمل عملية فقط
- تنوع في النصائح في كل مرة، لا تكرر نفس النوع مرتين في نفس الرد
- استند على الأرقام الحقيقية من البيانات المقدمة
- أسلوب ودي، مشجع، ومباشر

أنواع النصائح المتاحة (اختار 4 أنواع مختلفة في كل رد):
• top_product — تحليل الأكثر مبيعاً وكيف تزيد الاستفادة منه
• best_customer — أفضل العملاء وأفكار لمكافأتهم والاحتفاظ بهم
• bundle_idea — اقتراح باقة أو عرض تجميعي بناءً على المنتجات
• marketing — فكرة تسويقية مناسبة للمنتجات والموسم الحالي
• stock_alert — تنبيه مخزون منخفض وأولوية الشراء
• slow_mover — منتج راكد وطريقة ذكية لتصريفه
• trend — مقارنة المبيعات واتجاه النمو أو التراجع

رد بـ JSON فقط بدون أي نص خارجه — بالضبط 4 عناصر بهذا الشكل:
[{"type":"top_product","title":"عنوان قصير","body":"النصيحة هنا"},{"type":"...","title":"...","body":"..."},...]"""

    def _get_or_create_profile(self) -> "AIProfile":
        profile, _ = AIProfile.objects.get_or_create(
            name=self.PROFILE_NAME,
            defaults={
                'is_active': False,
                'system_instruction': self.SYSTEM_PROMPT,
                'model_id': 'gemini-2.5-flash',
                'temperature': 0.9,
                'max_output_tokens': 1200,
                'enabled_tools': [],
                'global_knowledge': False,
            },
        )
        return profile

    def _gather_context(self, store) -> str:
        from datetime import date, timedelta
        from django.db.models import Count, F, Sum

        from django.utils import timezone as tz
        from finance.models import SalesInvoice, SalesInvoiceItem
        from inventory.models import StockLevel, StorageStock
        from users.models import Customer

        now   = tz.now()
        ago30 = now - timedelta(days=30)
        ago60 = now - timedelta(days=60)

        posted = SalesInvoice.Status.POSTED

        top_products = list(
            SalesInvoiceItem.objects
            .filter(
                invoice__store=store, invoice__status=posted,
                invoice__date__gte=ago30,
            )
            .values('variant__product__name')
            .annotate(qty=Sum('quantity'), rev=Sum(F('quantity') * F('unit_price')))
            .order_by('-qty')[:6]
        )

        top_customers = list(
            SalesInvoice.objects
            .filter(
                store=store, status=posted, date__gte=ago30,
                is_deleted=False, customer__isnull=False,
            )
            .values('customer__name')
            .annotate(spent=Sum('grand_total'), visits=Count('id'))
            .order_by('-spent')[:4]
        )

        now_sales = SalesInvoice.objects.filter(
            store=store, status=posted, date__gte=ago30, is_deleted=False,
        ).aggregate(total=Sum('grand_total'), cnt=Count('id'))

        prev_sales = SalesInvoice.objects.filter(
            store=store, status=posted,
            date__gte=ago60, date__lt=ago30, is_deleted=False,
        ).aggregate(total=Sum('grand_total'), cnt=Count('id'))

        low_stock = list(
            StockLevel.objects
            .filter(
                variant__product__store=store,
                variant__is_deleted=False,
                variant__reorder_level__gt=0,
                quantity__lte=F('variant__reorder_level'),
            )
            .values('variant__product__name', 'quantity', 'variant__reorder_level')[:8]
        )

        storage_items = list(
            StorageStock.objects
            .filter(store=store, is_deleted=False, quantity_remaining__gt=0)
            .values('variant__product__name')
            .annotate(total=Sum('quantity_remaining'))
            .order_by('-total')[:5]
        )

        customer_count = Customer.objects.filter(store=store, is_deleted=False).count()

        lines = [
            f"اسم المتجر: {store.name}",
            f"تاريخ اليوم: {today.strftime('%Y-%m-%d')}",
            "",
            "=== المبيعات آخر 30 يوم ===",
            f"الإجمالي: {now_sales['total'] or 0:.2f}  |  الفواتير: {now_sales['cnt'] or 0}",
            f"الفترة السابقة (60-30 يوم): {prev_sales['total'] or 0:.2f}  |  الفواتير: {prev_sales['cnt'] or 0}",
            f"إجمالي العملاء: {customer_count}",
            "",
            "=== أكثر المنتجات مبيعاً ===",
        ]
        for p in top_products:
            lines.append(f"- {p['variant__product__name']}: {p['qty']} قطعة، إيراد {p['rev']:.2f}")

        lines += ["", "=== أفضل العملاء ==="]
        if top_customers:
            for c in top_customers:
                lines.append(f"- {c['customer__name']}: {c['spent']:.2f} ({c['visits']} زيارة)")
        else:
            lines.append("لا توجد مبيعات بعملاء مسجلين في هذه الفترة")

        lines += ["", "=== المخزون المنخفض ==="]
        if low_stock:
            for item in low_stock:
                lines.append(f"- {item['variant__product__name']}: متاح {item['quantity']}، حد الطلب {item['variant__reorder_level']}")
        else:
            lines.append("لا توجد منتجات دون حد الطلب")

        if storage_items:
            lines += ["", "=== البضاعة في التخزين ==="]
            for s in storage_items:
                lines.append(f"- {s['variant__product__name']}: {s['total']} قطعة")

        return "\n".join(lines)

    def get(self, request):
        store = getattr(request.user, 'store', None)
        if not store:
            return Response({'error': 'no store'}, status=400)

        try:
            settings_row = AISettings.load()
            service = GeminiService(settings_row.gemini_api_key)
        except (NoApiKey, Exception):
            return Response({'error': 'AI not configured'}, status=503)

        profile = self._get_or_create_profile()
        store_context = self._gather_context(store)

        user_prompt = (
            f"بناءً على بيانات المتجر التالية، قدم 4 نصائح تجارية متنوعة ومفيدة:\n\n"
            f"{store_context}\n\n"
            f"تذكر: رد بـ JSON فقط كما هو محدد، 4 عناصر، أنواع مختلفة."
        )

        try:
            from google import genai as _genai

            response = service.client.models.generate_content(
                model=profile.model_id or 'gemini-2.5-flash',
                contents=user_prompt,
                config=_genai.types.GenerateContentConfig(
                    system_instruction=profile.system_instruction or self.SYSTEM_PROMPT,
                    temperature=profile.temperature if profile.temperature is not None else 0.9,
                    max_output_tokens=profile.max_output_tokens or 1200,
                    response_mime_type='application/json',
                ),
            )

            raw = response.text or '[]'
            # Strip markdown fences if present
            if raw.strip().startswith('```'):
                raw = raw.strip().lstrip('`').split('\n', 1)[-1].rsplit('```', 1)[0]

            insights = json.loads(raw)
            if not isinstance(insights, list):
                raise ValueError("unexpected shape")

            # Normalise and cap at 4
            clean = [
                {
                    'type': str(item.get('type', 'insight')),
                    'title': str(item.get('title', '')),
                    'body': str(item.get('body', '')),
                }
                for item in insights[:4]
                if item.get('title') and item.get('body')
            ]
            return Response({'insights': clean})

        except Exception as e:
            logger.exception("V-Agent insights error: %s", e)
            return Response({'error': str(e)}, status=500)
