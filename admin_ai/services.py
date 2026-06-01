"""Gemini service layer.

Wraps the `google-genai` SDK. Key is loaded lazily from AISettings so the
admin can rotate it without restarting Django. All network calls stay
server-side — the key never reaches the frontend.

Public surface:
    GeminiService.from_settings()               -> instance (raises NoApiKey)
    .ping()                                     -> bool   (cheap connectivity check)
    .refresh_models()                           -> int    (rows written to AIModelCache)
    .chat_stream(profile, history, prompt, ...) -> generator yielding event dicts
    .embed(text)                                -> list[float] (text-embedding-004)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

import requests

from django.utils import timezone

from .models import AISettings, AIModelCache, AIProfile
from .registry import registry, ToolContext, ToolError

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = 'text-embedding-004'
DEFAULT_PING_MODEL = 'gemini-2.5-flash'


# ---------- exceptions ----------

class GeminiError(Exception):
    """Base class for Gemini service errors."""


class NoApiKey(GeminiError):
    """Raised when AISettings has no key configured."""


# ---------- service ----------

class GeminiService:
    """Thin wrapper around `google.genai.Client`.

    Instances are cheap to construct; do not cache across requests in case
    the admin rotates the key mid-session.
    """

    MODELS_URL = 'https://generativelanguage.googleapis.com/v1beta/models'

    def __init__(self, api_key: str):
        if not api_key:
            raise NoApiKey("Gemini API key is not configured.")
        self.api_key = api_key
        # Imported lazily so unit tests / migrations that never touch the
        # service don't pay the import cost.
        from google import genai
        self._genai = genai
        self.client = genai.Client(api_key=api_key)

    # ---- factory ----

    @classmethod
    def from_settings(cls) -> "GeminiService":
        settings_row = AISettings.load()
        return cls(settings_row.gemini_api_key)

    # ---- health ----

    def ping(self) -> Dict[str, Any]:
        """Lightweight check that the key is valid and the API is reachable.

        Used by the admin footer status indicator. Returns a small dict so
        callers can show a richer state than just on/off.
        """
        try:
            resp = requests.get(self.MODELS_URL, params={'key': self.api_key}, timeout=10)
            resp.raise_for_status()
            return {'ok': True}
        except Exception as e:  # noqa: BLE001 — surface any network / auth error
            logger.warning("Gemini ping failed: %s", e)
            return {'ok': False, 'error': str(e)}

    # ---- model catalog ----

    # Names that are NOT regular text chat models — skip them on discovery.
    # (image generation, text-to-speech, embeddings, realtime/bidi, special tool builds)
    _NON_CHAT_HINTS = ('image', 'tts', 'embedding', 'aqa', 'live', 'customtools',
                       'vision', 'robotics', 'computer-use')

    def refresh_models(self) -> Dict[str, Any]:
        """Ask Google (via the SDK) for its live model list and mirror it locally.

        Source of truth = Google's own `models.list()`. We keep every real Gemini
        chat model (supports generateContent, not an image/tts/embedding/realtime
        variant), under its REAL name. New models from Google appear automatically;
        deprecated ones disappear automatically. No hand-maintained list, and we
        deliberately do NOT track RPM/RPD (those aren't in the API — they depend on
        the billing tier — so they're left blank rather than faked).

        Returns the diff for the refresh modal: {'added': [...], 'kept': [...], 'removed': [...]}.
        """
        try:
            remote = list(self.client.models.list())
        except Exception as e:  # noqa: BLE001 — bad key / network → surface clearly
            raise GeminiError(f"Couldn't reach Gemini to list models: {e}") from e

        # Code-free overrides from the Misc page.
        settings_row = AISettings.load()
        extra  = set(settings_row.extra_model_list)   # force-include these
        hidden = settings_row.hidden_model_list       # substring-hide these

        seen_ids: List[str] = []
        added: List[str] = []
        kept: List[str] = []

        for m in remote:
            raw_name = getattr(m, 'name', '') or ''
            model_id = raw_name.split('/', 1)[1] if raw_name.startswith('models/') else raw_name
            if not model_id:
                continue
            low = model_id.lower()

            # Explicit hide always wins.
            if any(h in low for h in hidden):
                continue

            # A model the user pinned skips the auto-filters; everything else
            # must look like a normal Gemini chat model.
            if low not in extra:
                if 'gemini' not in low:
                    continue
                actions = getattr(m, 'supported_actions', None) or []
                if 'generateContent' not in actions:
                    continue
                if any(hint in low for hint in self._NON_CHAT_HINTS):
                    continue

            seen_ids.append(model_id)
            _, created = AIModelCache.objects.update_or_create(
                model_id=model_id,
                defaults=dict(
                    display_name=getattr(m, 'display_name', '') or model_id,
                    description=getattr(m, 'description', '') or '',
                    tokens=getattr(m, 'input_token_limit', None),   # real context window
                    rpm=None, rpd=None,                              # not exposed by the API
                    supports_thinking='lite' not in low,            # lites generally can't think
                    supports_grounding=True,
                    supports_vision=True,
                    supports_audio=True,
                ),
            )
            (added if created else kept).append(model_id)

        # Anything we had cached that Google no longer lists → drop it.
        removed_qs = AIModelCache.objects.exclude(model_id__in=seen_ids)
        removed = list(removed_qs.values_list('model_id', flat=True))
        removed_qs.delete()

        return {
            'added': added,
            'kept': kept,
            'removed': removed,
            'refreshed_at': timezone.now().isoformat(),
        }

    # ---- embeddings ----

    def embed(self, text: str) -> List[float]:
        """Embed a single string with text-embedding-004 (768-dim)."""
        if not text:
            return []
        try:
            result = self.client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text,
            )
        except Exception as e:  # noqa: BLE001
            raise GeminiError(f"Embedding failed: {e}") from e

        # SDK returns result.embeddings = [Embedding(values=[...])]
        embeddings = getattr(result, 'embeddings', None) or []
        if not embeddings:
            raise GeminiError("Empty embeddings response.")
        values = getattr(embeddings[0], 'values', None) or []
        return list(values)

    # ---- chat ----

    def chat_stream(
        self,
        profile: AIProfile,
        history: List[Dict[str, Any]],
        prompt: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
        tool_context: Optional[ToolContext] = None,
    ) -> Iterable[Dict[str, Any]]:
        """Stream a single user turn back as SSE events.

        Yields dicts with these shapes:
            {'event': 'token',  'text': '...'}
            {'event': 'tool',   'name': '...', 'args': {...}, 'result': {...}}
            {'event': 'done',   'usage': {...}}
            {'event': 'error',  'message': '...'}

        `history` is a list of `{role, content}` from prior turns (already
        flattened by the view layer). `attachments` is a list of
        `{mime_type, data}` dicts for inline image / audio bytes.
        """
        if not profile.model_id:
            yield {'event': 'error', 'message': 'Active profile has no model selected.'}
            return

        try:
            contents = self._build_contents(history, prompt, attachments)
            config  = self._build_generate_config(profile)
        except Exception as e:  # noqa: BLE001
            yield {'event': 'error', 'message': f'Bad request: {e}'}
            return

        try:
            stream = self.client.models.generate_content_stream(
                model=profile.model_id,
                contents=contents,
                config=config,
            )
        except Exception as e:  # noqa: BLE001
            yield {'event': 'error', 'message': f'Gemini call failed: {e}'}
            return

        last_usage: Dict[str, Any] = {}
        for chunk in stream:
            # Function calls
            for fc in self._extract_function_calls(chunk):
                tool_result = self._invoke_tool(fc, tool_context)
                yield {
                    'event': 'tool',
                    'name': fc.get('name'),
                    'args': fc.get('args') or {},
                    'result': tool_result,
                }
            # Token deltas
            text = getattr(chunk, 'text', None)
            if text:
                yield {'event': 'token', 'text': text}
            # Usage metadata appears on the final chunk
            usage = getattr(chunk, 'usage_metadata', None)
            if usage:
                last_usage = {
                    'input_tokens': getattr(usage, 'prompt_token_count', None),
                    'output_tokens': getattr(usage, 'candidates_token_count', None),
                    'total_tokens': getattr(usage, 'total_token_count', None),
                }

        yield {'event': 'done', 'usage': last_usage}

    # ---- internals ----

    def _build_contents(
        self,
        history: List[Dict[str, Any]],
        prompt: str,
        attachments: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        contents: List[Dict[str, Any]] = []
        for turn in history:
            role = turn.get('role') or 'user'
            # Gemini uses 'user' / 'model' roles; map our enum.
            mapped_role = 'model' if role.upper() == 'MODEL' else 'user'
            contents.append({
                'role': mapped_role,
                'parts': [{'text': turn.get('content') or ''}],
            })

        parts: List[Dict[str, Any]] = [{'text': prompt}]
        for att in attachments or []:
            mime = att.get('mime_type')
            data = att.get('data')
            if mime and data:
                parts.append({'inline_data': {'mime_type': mime, 'data': data}})
        contents.append({'role': 'user', 'parts': parts})
        return contents

    def _build_generate_config(self, profile: AIProfile) -> Dict[str, Any]:
        """Construct the `config=` payload for generate_content_stream.

        Blank/None values are dropped so the model defaults take over. Tool
        declarations are pulled from the registry, optionally filtered by
        the profile's `enabled_tools` allowlist.
        """
        cfg: Dict[str, Any] = {}
        if profile.system_instruction:
            cfg['system_instruction'] = profile.system_instruction
        if profile.temperature is not None:
            cfg['temperature'] = profile.temperature
        if profile.top_p is not None:
            cfg['top_p'] = profile.top_p
        if profile.top_k is not None:
            cfg['top_k'] = profile.top_k
        if profile.max_output_tokens:
            cfg['max_output_tokens'] = profile.max_output_tokens

        declarations = registry.declarations_for(profile.enabled_tools or None)
        if declarations:
            cfg['tools'] = [{'function_declarations': declarations}]
        return cfg

    def _extract_function_calls(self, chunk) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        candidates = getattr(chunk, 'candidates', None) or []
        for cand in candidates:
            content = getattr(cand, 'content', None)
            if not content:
                continue
            for part in getattr(content, 'parts', None) or []:
                fc = getattr(part, 'function_call', None)
                if fc:
                    calls.append({
                        'name': getattr(fc, 'name', None),
                        'args': dict(getattr(fc, 'args', None) or {}),
                    })
        return calls

    def _invoke_tool(self, fc: Dict[str, Any], context: Optional[ToolContext]) -> Dict[str, Any]:
        name = fc.get('name')
        args = fc.get('args') or {}
        if context is None:
            return {'error': 'No tool context — refusing to execute write tools.'}
        try:
            result = registry.invoke(name, args, context)
            return {'ok': True, 'data': result}
        except ToolError as e:
            return {'ok': False, 'error': str(e)}
        except Exception as e:  # noqa: BLE001
            logger.exception("Tool %s raised", name)
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
