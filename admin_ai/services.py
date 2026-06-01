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

    # Static, hand-maintained catalog of the models we actually use.
    # WHY static: on the free tier the live `/v1beta/models?key=` list endpoint
    # is unreliable — it intermittently omits preview / allowlisted models (e.g.
    # the gemini-3.x family) even though generateContent on them works fine.
    # Reconciling against the live list therefore deletes good models and wipes
    # the rate limits. So this catalog is the source of truth; Refresh (manual or
    # the 24h job) just re-asserts it. Edit THIS dict when Google changes the lineup.
    #   model_id: (rpm, rpd, tpm, thinking, grounding, vision, audio, display_name)
    MODEL_CATALOG = {
        'gemini-3.5-flash':       (15, 1500, 1_000_000, True,  True, True, True, 'Gemini 3.5 Flash'),
        'gemini-3-flash-preview': (15, 1500, 1_000_000, True,  True, True, True, 'Gemini 3 Flash (Preview)'),
        'gemini-3.1-flash-lite':  (15, 1500, 1_000_000, True,  True, True, True, 'Gemini 3.1 Flash-Lite'),
        'gemini-2.5-pro':         (2,    50,    32_000, True,  True, True, True, 'Gemini 2.5 Pro'),
        'gemini-2.5-flash':       (15, 1500, 1_000_000, True,  True, True, True, 'Gemini 2.5 Flash'),
        'gemini-2.5-flash-lite':  (15, 1500, 1_000_000, False, True, True, True, 'Gemini 2.5 Flash-Lite'),
    }

    def refresh_models(self) -> Dict[str, Any]:
        """Reconcile AIModelCache against the static MODEL_CATALOG.

        Free-tier-safe: does NOT depend on the live `/models` list (which flakily
        omits preview models). It only PINGS the API first to confirm the key is
        valid/reachable, then writes the known-good catalog. Returns a diff summary
        for the C4 refresh modal: {'added': [...], 'kept': [...], 'removed': [...]}.
        """
        # Confirm the key works before touching the cache — but don't let a flaky
        # list response decide which models exist.
        health = self.ping()
        if not health.get('ok'):
            raise GeminiError(f"Gemini key/API unreachable: {health.get('error', 'unknown error')}")

        seen_ids: List[str] = []
        added: List[str] = []
        kept: List[str] = []

        for model_id, (rpm, rpd, tpm, thinking, grounding, vision, audio, display) in self.MODEL_CATALOG.items():
            seen_ids.append(model_id)
            _, created = AIModelCache.objects.update_or_create(
                model_id=model_id,
                defaults=dict(
                    display_name=display,
                    rpm=rpm, rpd=rpd, tokens=tpm,
                    supports_thinking=thinking,
                    supports_grounding=grounding,
                    supports_vision=vision,
                    supports_audio=audio,
                ),
            )
            (added if created else kept).append(model_id)

        # Drop anything cached that's no longer in the catalog.
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
