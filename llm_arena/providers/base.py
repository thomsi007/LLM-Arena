"""Provider-independent LLM types: configuration, results and error hierarchy.

Every concrete provider (llama-server / OpenAI-compatible, or anything added
later) implements :class:`LLMProvider` and raises only :class:`LLMError`
subclasses, so the workflows never depend on transport details.
"""

from __future__ import annotations

import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Optional


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class LLMConfig:
    slot: str = "A"
    name: str = "LLM A"
    provider: str = "openai_compat"
    base_url: str = "http://127.0.0.1:8080"
    model: str = ""              # empty / "auto" => automatic detection
    api_key: str = ""
    timeout: float = 180.0       # total seconds allowed for one request
    temperature: float = 0.7
    max_tokens: int = 2048
    system_prompt: str = ""
    stream: bool = True
    retries: int = 1             # extra attempts on transient errors
    context_chars: int = 16000   # rough prompt budget used when clipping context
    use_system_proxy: bool = False
    disable_thinking: bool = False  # ask reasoning models (Qwen3, …) to skip the <think> phase

    @classmethod
    def from_dict(cls, data: dict | None, slot: str | None = None) -> "LLMConfig":
        cfg = cls()
        if slot:
            cfg.slot = slot
            cfg.name = f"LLM {slot}"
        for f in fields(cls):
            if not data or f.name not in data or data[f.name] is None:
                continue
            value = data[f.name]
            try:
                if f.type in ("float", float):
                    value = float(value)
                elif f.type in ("int", int):
                    value = int(float(value))
                elif f.type in ("bool", bool):
                    value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
                else:
                    value = str(value)
            except (TypeError, ValueError):
                continue
            setattr(cfg, f.name, value)
        if slot:
            cfg.slot = slot
        cfg.timeout = max(1.0, cfg.timeout)
        cfg.max_tokens = max(1, cfg.max_tokens)
        cfg.retries = max(0, min(cfg.retries, 5))
        cfg.temperature = max(0.0, min(cfg.temperature, 2.0))
        cfg.context_chars = max(2000, cfg.context_chars)
        cfg.base_url = normalize_base_url(cfg.base_url)
        return cfg

    def to_dict(self, include_secret: bool = True) -> dict:
        d = asdict(self)
        if not include_secret:
            d["api_key"] = "" if not self.api_key else "***"
        return d

    @property
    def wants_autodetect(self) -> bool:
        return not self.model or self.model.strip().lower() == "auto"


def normalize_base_url(url: str) -> str:
    """Accept what users paste: full endpoint URLs, trailing slashes, missing scheme."""
    import re
    u = (url or "").strip().rstrip("/")
    u = re.sub(r"/(chat/completions|completions|models|embeddings)$", "", u, flags=re.I)
    if u and not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    return u


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class LLMError(Exception):
    """Base class of every communication / model error."""

    kind = "llm_error"
    retryable = False
    label = "LLM hiba"

    def __init__(self, message: str, *, status: int | None = None,
                 detail: str | None = None, partial: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail
        self.partial = partial

    def to_dict(self) -> dict:
        from ..errors import hint_for
        return {
            "kind": self.kind,
            "label": self.label,
            "message": self.message,
            "hint": hint_for(self.kind, self.status),
            "status": self.status,
            "detail": (self.detail or "")[:2000] or None,
            "retryable": self.retryable,
        }


class EndpointUnreachable(LLMError):
    kind, retryable, label = "unreachable", True, "Az endpoint nem érhető el"


class LLMTimeout(LLMError):
    kind, retryable, label = "timeout", True, "Időtúllépés"


class HTTPStatusError(LLMError):
    kind, label = "http_error", "HTTP hiba"

    @property
    def retryable(self) -> bool:  # type: ignore[override]
        return bool(self.status and (self.status >= 500 or self.status == 429))


class InvalidResponse(LLMError):
    kind, label = "invalid_json", "Hibás JSON válasz"


class EmptyResponse(LLMError):
    kind, retryable, label = "empty_response", True, "Üres válasz"


class ModelError(LLMError):
    kind, label = "model_error", "Modellhiba"


class ConnectionInterrupted(LLMError):
    kind, retryable, label = "interrupted", True, "Megszakadt kapcsolat"


class ToolsUnsupported(LLMError):
    kind, label = "tools_unsupported", "Az endpoint nem támogatja a natív tool-hívást"


class Cancelled(LLMError):
    kind, label = "cancelled", "Megszakítva"


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #

class CancelToken:
    """Thread-safe cancellation flag with callbacks (used to close sockets)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next = 0

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = list(self._callbacks.values())
        for cb in callbacks:
            try:
                cb()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def is_set(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled("A műveletet a felhasználó megszakította.")

    def add_callback(self, cb: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            key = self._next
            self._next += 1
            self._callbacks[key] = cb
        if self._event.is_set():
            cb()

        def remove() -> None:
            with self._lock:
                self._callbacks.pop(key, None)
        return remove

    def wait(self, seconds: float) -> bool:
        """Sleep that wakes up early on cancel. Returns True if cancelled."""
        return self._event.wait(seconds)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

TokenCallback = Callable[[str, str], None]   # (delta, kind) kind: content|reasoning|reset


@dataclass
class ChatResult:
    content: str
    model: str = ""
    reasoning: str = ""
    finish_reason: str | None = None
    latency: float = 0.0          # seconds, whole request
    ttft: float | None = None     # seconds until first token (streaming)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    tokens_estimated: bool = False
    tokens_per_second: float | None = None
    attempts: int = 1
    streamed: bool = False
    raw_usage: dict = field(default_factory=dict)
    tool_calls: list = field(default_factory=list)   # native OpenAI-style tool calls


def new_message(*, slot: str, model: str, role: str, workflow: str,
                stage: str = "", round: int = 0, title: str = "") -> dict:
    """Structured record of one model answer (also used as a placeholder while streaming)."""
    return {
        "id": uuid.uuid4().hex[:12],
        "slot": slot,
        "model": model,
        "role": role,
        "workflow": workflow,
        "stage": stage,
        "round": round,
        "title": title,
        "content": "",
        "reasoning": "",
        "latency": 0.0,
        "ttft": None,
        "tokens": 0,
        "prompt_tokens": None,
        "total_tokens": None,
        "tokens_estimated": False,
        "tokens_per_second": None,
        "finish_reason": None,
        "attempts": 0,
        "status": "streaming",
        "error": None,
        "structured": None,
        "created": time.time(),
    }


def fill_message(msg: dict, result: ChatResult) -> dict:
    msg.update({
        "model": result.model or msg.get("model", ""),
        "content": result.content,
        "reasoning": result.reasoning,
        "latency": round(result.latency, 3),
        "ttft": None if result.ttft is None else round(result.ttft, 3),
        "tokens": result.completion_tokens or 0,
        "prompt_tokens": result.prompt_tokens,
        "total_tokens": result.total_tokens,
        "tokens_estimated": result.tokens_estimated,
        "tokens_per_second": None if result.tokens_per_second is None else round(result.tokens_per_second, 2),
        "finish_reason": result.finish_reason,
        "attempts": result.attempts,
        "status": "done",
    })
    return msg


# --------------------------------------------------------------------------- #
# Provider interface
# --------------------------------------------------------------------------- #

class LLMProvider(ABC):
    """Interface every endpoint implementation must provide."""

    provider_type = "abstract"

    def __init__(self, config: LLMConfig):
        self.config = config

    @abstractmethod
    def health(self) -> dict:
        """Cheap liveness probe. Returns {"ok": bool, ...}; raises LLMError when unreachable."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Model identifiers served by the endpoint."""

    def detect_model(self) -> str:
        models = self.list_models()
        return models[0] if models else ""

    @abstractmethod
    def chat(self, messages: list[dict], *, on_token: Optional[TokenCallback] = None,
             cancel: Optional[CancelToken] = None, **overrides: Any) -> ChatResult:
        """Run a chat completion (streaming when enabled)."""

    def test_connection(self, *, probe_completion: bool = True) -> dict:
        """Step by step connection check used by the UI."""
        steps: list[dict] = []
        ok = True
        model = self.config.model if not self.config.wants_autodetect else ""
        t0 = time.monotonic()
        try:
            h = self.health()
            steps.append({"step": "health", "ok": bool(h.get("ok")), "info": h.get("info", "")})
            ok = ok and bool(h.get("ok"))
        except LLMError as e:
            steps.append({"step": "health", "ok": False, "error": e.to_dict()})
            return {"ok": False, "steps": steps, "model": model,
                    "latency": round(time.monotonic() - t0, 3)}
        models: list[str] = []
        try:
            models = self.list_models()
            steps.append({"step": "models", "ok": True, "info": ", ".join(models) or "(nincs lista)"})
            if not model and models:
                model = models[0]
        except LLMError as e:
            steps.append({"step": "models", "ok": False, "error": e.to_dict()})
        if probe_completion and ok:
            try:
                # Generous token budget + thinking disabled: reasoning models otherwise
                # spend a tiny budget on <think> and return an empty answer.
                r = self.chat([{"role": "user", "content": "Reply with the single word: pong /no_think"}],
                              max_tokens=512, temperature=0.0, stream=False, retries=0, disable_thinking=True)
                steps.append({"step": "completion", "ok": True,
                              "info": f"{r.content.strip()[:60]!r} ({r.latency:.2f}s)"})
                model = model or r.model
            except EmptyResponse as e:
                if e.detail == "reasoning_only":
                    steps.append({"step": "completion", "ok": True,
                                  "info": "a modell válaszolt, de csak gondolkodott (reasoning modell) – a kapcsolat működik"})
                else:
                    ok = False
                    steps.append({"step": "completion", "ok": False, "error": e.to_dict()})
            except LLMError as e:
                ok = False
                steps.append({"step": "completion", "ok": False, "error": e.to_dict()})
        return {"ok": ok, "steps": steps, "model": model, "models": models,
                "latency": round(time.monotonic() - t0, 3)}
