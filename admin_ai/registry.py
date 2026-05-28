"""Tool registry for the Admin AI.

C3 will populate this with ~40 read/write tools. C1 only ships the
infrastructure: a decorator-based registry, JSON-schema declarations
suitable for Gemini function-calling, and acting-store auto-scoping
that wraps every registered tool.

Usage (C3):

    from admin_ai.registry import tool

    @tool(
        name='list_products',
        description='List products in the acting store.',
        parameters={'type': 'object', 'properties': {
            'category': {'type': 'string'},
        }},
        write=False,
    )
    def list_products(context, category=None):
        # context.store is auto-resolved from request.user.store at call time.
        ...

The first arg is always a `ToolContext`. The model schema (sent to Gemini)
hides that arg — only declared `parameters` are exposed.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.models import Store


@dataclass
class ToolContext:
    """Per-invocation context handed to every tool.

    `user` — the authenticated sudo user.
    `store` — the active "acting store" (None when sudo isn't impersonating).
              Resolved from request.user.store, which VendoryaJWTAuthentication
              already populates from the X-Store-ID header.
    `request` — full DRF request, in case a tool needs IP / headers.
    """

    user: Any
    store: Optional[Store]
    request: Any = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]            # JSON schema (Gemini format)
    func: Callable[..., Any]
    write: bool = False                   # True = mutating (audit + auto-scope to acting store)
    requires_store: bool = False          # True = error out if context.store is None

    def as_gemini_declaration(self) -> Dict[str, Any]:
        """Render as a Gemini `FunctionDeclaration` dict."""
        return {
            'name': self.name,
            'description': self.description,
            'parameters': self.parameters,
        }


class ToolRegistry:
    """Process-wide registry. Populated at app load via `@tool(...)`."""

    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool {spec.name!r} already registered.")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def all(self) -> List[ToolSpec]:
        return list(self._tools.values())

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def declarations_for(self, names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Gemini `function_declarations` payload, filtered by `names`.

        Empty / None `names` means "all registered" — matches AIProfile.enabled_tools
        semantics where an empty list means no filter.
        """
        if not names:
            specs = self.all()
        else:
            specs = [self._tools[n] for n in names if n in self._tools]
        return [s.as_gemini_declaration() for s in specs]

    def invoke(self, name: str, args: Dict[str, Any], context: ToolContext) -> Any:
        """Run a registered tool with the given args + context.

        Raises `ToolNotFound` / `ToolValidationError` on misuse so the chat
        loop can surface a structured error back to the model.
        """
        spec = self._tools.get(name)
        if spec is None:
            raise ToolNotFound(f"Tool {name!r} is not registered.")
        if spec.requires_store and context.store is None:
            raise ToolValidationError(
                f"Tool {name!r} requires an acting store. "
                f"Send the X-Store-ID header to pick one."
            )
        # The function signature is (context, **kwargs). Trust schema validation
        # to be enforced by the model layer when possible; defensively coerce
        # unexpected types here.
        if not isinstance(args, dict):
            raise ToolValidationError(f"Tool args must be an object, got {type(args).__name__}.")
        return spec.func(context, **args)


# ---------- exceptions ----------

class ToolError(Exception):
    """Base class for registry errors that should be reported back to the model."""


class ToolNotFound(ToolError):
    pass


class ToolValidationError(ToolError):
    pass


# ---------- singleton + decorator ----------

registry = ToolRegistry()


def tool(*, name: str, description: str, parameters: Optional[Dict[str, Any]] = None,
         write: bool = False, requires_store: bool = False) -> Callable:
    """Decorator that registers a function as a tool.

    `parameters` is a JSON schema object describing the tool's args
    (Gemini function-calling format). A blank dict means "no arguments".
    """

    def deco(func: Callable) -> Callable:
        registry.register(ToolSpec(
            name=name,
            description=description,
            parameters=parameters or {'type': 'object', 'properties': {}},
            func=func,
            write=write,
            requires_store=requires_store,
        ))
        return func

    return deco
