"""OpenAI-compatible chat completion client (llama.cpp `llama-server`, vLLM,
LM Studio, Ollama's /v1 layer, ...). Standard library only."""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .base import (
    Cancelled, CancelToken, ChatResult, ConnectionInterrupted, EmptyResponse,
    EndpointUnreachable, HTTPStatusError, InvalidResponse, LLMConfig, LLMError,
    LLMProvider, LLMTimeout, ModelError, TokenCallback, ToolsUnsupported,
)

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.S | re.I)

# Model detection is cached per endpoint to avoid a /v1/models call per request.
_MODEL_CACHE: dict[str, tuple[float, str]] = {}
_MODEL_CACHE_TTL = 300.0


def split_thinking(content: str) -> tuple[str, str]:
    """Move <think>...</think> sections (reasoning models) out of the answer."""
    thoughts = _THINK_RE.findall(content)
    if not thoughts:
        # Unterminated think block (e.g. max_tokens hit while thinking).
        low = content.lower()
        if low.lstrip().startswith("<think>") and "</think>" not in low:
            return "", content.split(">", 1)[1]
        return content, ""
    return _THINK_RE.sub("", content).strip(), "\n".join(t.strip() for t in thoughts)


def _error_message(body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or err)
        return str(err)
    except (ValueError, AttributeError):
        return text.strip()[:500] or "(üres törzs)"


class OpenAICompatProvider(LLMProvider):
    provider_type = "openai_compat"

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        handlers = [] if config.use_system_proxy else [urllib.request.ProxyHandler({})]
        self._opener = urllib.request.build_opener(*handlers)

    # ------------------------------------------------------------------ http
    def _url(self, path: str) -> str:
        base = self.config.base_url.strip().rstrip("/")
        if not re.match(r"^https?://", base, re.I):
            base = "http://" + base
        if base.endswith("/v1") and path.startswith("/v1/"):
            path = path[3:]
        return base + path

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.config.api_key:
            h["Authorization"] = f"Bearer {self.config.api_key}"
        return h

    def _open(self, method: str, path: str, body: Any = None, timeout: float | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._url(path), data=data, method=method, headers=self._headers())
        timeout = timeout or self.config.timeout
        try:
            return self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()[:4000]
            except Exception:  # noqa: BLE001
                raw = b""
            msg = _error_message(raw)
            raise HTTPStatusError(f"HTTP {e.code}: {msg}", status=e.code, detail=raw.decode("utf-8", "replace"))
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise LLMTimeout(f"Időtúllépés a kapcsolódáskor ({timeout:.0f}s).")
            raise EndpointUnreachable(f"Nem érhető el: {self._url(path)} ({e.reason})")
        except (socket.timeout, TimeoutError):
            raise LLMTimeout(f"Időtúllépés ({timeout:.0f}s).")
        except (ConnectionError, http.client.HTTPException, OSError) as e:
            raise EndpointUnreachable(f"Kapcsolódási hiba: {e}")

    def _get_json(self, path: str, timeout: float = 10.0) -> Any:
        resp = self._open("GET", path, timeout=timeout)
        try:
            raw = resp.read()
        except (socket.timeout, TimeoutError):
            raise LLMTimeout("Időtúllépés olvasás közben.")
        except (ConnectionError, http.client.HTTPException, OSError) as e:
            raise ConnectionInterrupted(f"Megszakadt kapcsolat: {e}")
        finally:
            resp.close()
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise InvalidResponse("A szerver nem JSON választ adott.", detail=raw[:1000].decode("utf-8", "replace"))

    # ------------------------------------------------------------ discovery
    def health(self) -> dict:
        try:
            data = self._get_json("/health", timeout=min(10.0, self.config.timeout))
            status = str(data.get("status", "ok")) if isinstance(data, dict) else "ok"
            return {"ok": status in ("ok", "no slot available", "ready"), "info": f"/health: {status}"}
        except HTTPStatusError as e:
            if e.status == 503:
                return {"ok": False, "info": "A modell még töltődik (503)."}
            if e.status in (404, 405, 401):
                # Not a llama-server (or protected); fall back to /v1/models.
                self._get_json("/v1/models", timeout=min(10.0, self.config.timeout))
                return {"ok": True, "info": "/v1/models elérhető"}
            raise
        except InvalidResponse:
            return {"ok": True, "info": "/health válaszolt (nem JSON)"}

    def list_models(self) -> list[str]:
        models: list[str] = []
        try:
            data = self._get_json("/v1/models", timeout=min(15.0, self.config.timeout))
            items = (data.get("data") or data.get("models") or []) if isinstance(data, dict) else data
            if not isinstance(items, list):
                items = []
            for item in items:
                if isinstance(item, dict):
                    mid = item.get("id") or item.get("name") or item.get("model")
                    if mid and mid not in models:
                        models.append(str(mid))
        except (HTTPStatusError, InvalidResponse):
            pass
        if not models:
            # llama-server specific fallback
            try:
                props = self._get_json("/props", timeout=min(10.0, self.config.timeout))
                if isinstance(props, dict):
                    name = props.get("model_alias") or props.get("model_path") or ""
                    if name:
                        models.append(str(name).replace("\\", "/").split("/")[-1])
            except (HTTPStatusError, InvalidResponse):
                pass
        return models

    def resolve_model(self) -> str:
        if not self.config.wants_autodetect:
            return self.config.model.strip()
        key = self.config.base_url
        cached = _MODEL_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _MODEL_CACHE_TTL:
            return cached[1]
        try:
            model = self.detect_model()
        except LLMError:
            model = ""
        if model:
            _MODEL_CACHE[key] = (time.monotonic(), model)
        return model

    # ----------------------------------------------------------------- chat
    def chat(self, messages: list[dict], *, on_token: Optional[TokenCallback] = None,
             cancel: Optional[CancelToken] = None, **overrides: Any) -> ChatResult:
        retries = int(overrides.pop("retries", self.config.retries))
        attempt = 0
        while True:
            attempt += 1
            if cancel:
                cancel.raise_if_cancelled()
            try:
                result = self._chat_once(messages, on_token=on_token, cancel=cancel, **overrides)
                result.attempts = attempt
                return result
            except Cancelled:
                raise
            except LLMError as e:
                if cancel and cancel.is_set():
                    raise Cancelled("A műveletet a felhasználó megszakította.")
                if not e.retryable or attempt > retries:
                    raise
                if on_token:
                    on_token("", "reset")
                delay = min(2 ** (attempt - 1), 8)
                if cancel and cancel.wait(delay):
                    raise Cancelled("A műveletet a felhasználó megszakította.")
                if not cancel:
                    time.sleep(delay)

    def _payload(self, messages: list[dict], stream: bool, overrides: dict) -> dict:
        cfg = self.config
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": float(overrides.get("temperature", cfg.temperature)),
            "max_tokens": int(overrides.get("max_tokens", cfg.max_tokens)),
            "stream": stream,
        }
        model = self.resolve_model()
        if model:
            payload["model"] = model
        if overrides.get("disable_thinking", cfg.disable_thinking) and overrides.get("_template_kwargs", True):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if stream and overrides.get("_stream_options", True):
            payload["stream_options"] = {"include_usage": True}
        if overrides.get("tools"):
            payload["tools"] = overrides["tools"]
            payload["tool_choice"] = "auto"
        if overrides.get("response_format"):
            payload["response_format"] = overrides["response_format"]
        return payload

    def _chat_once(self, messages: list[dict], *, on_token: Optional[TokenCallback],
                   cancel: Optional[CancelToken], **overrides: Any) -> ChatResult:
        stream = bool(overrides.get("stream", self.config.stream))
        payload = self._payload(messages, stream, overrides)
        timeout = float(overrides.get("timeout", self.config.timeout))
        start = time.monotonic()
        try:
            resp = self._open("POST", "/v1/chat/completions", payload, timeout=timeout)
        except HTTPStatusError as e:
            if e.status in (400, 422) and "chat_template_kwargs" in payload and \
                    "chat_template_kwargs" in (e.message or "") + (e.detail or ""):
                overrides = dict(overrides, _template_kwargs=False)
                return self._chat_once(messages, on_token=on_token, cancel=cancel, **overrides)
            if "tools" in payload and e.status in (400, 422, 500, 501) and re.search(
                    r"tool|jinja|function", (e.message or "") + (e.detail or ""), re.I):
                raise ToolsUnsupported(f"A szerver elutasította a tools paramétert: {e.message}", status=e.status)
            if stream and e.status == 400 and "stream_options" in (e.message or ""):
                overrides = dict(overrides, _stream_options=False)
                return self._chat_once(messages, on_token=on_token, cancel=cancel, **overrides)
            raise
        remove_cb = cancel.add_callback(lambda: _abort(resp)) if cancel else (lambda: None)
        try:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if stream and "text/event-stream" in ctype:
                result = self._read_stream(resp, start, timeout, on_token, cancel)
            else:
                result = self._read_plain(resp, start, cancel)
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001 - anything from a closed socket
            if cancel and cancel.is_set():
                raise Cancelled("A műveletet a felhasználó megszakította.")
            raise ConnectionInterrupted(f"Megszakadt kapcsolat: {e}")
        finally:
            remove_cb()
            _safe_close(resp)
        return self._finalize(result, payload)

    def _read_plain(self, resp, start: float, cancel: Optional[CancelToken]) -> ChatResult:
        try:
            raw = resp.read()
        except (socket.timeout, TimeoutError):
            raise LLMTimeout("Időtúllépés a válasz olvasása közben.")
        except (ConnectionError, http.client.IncompleteRead, OSError) as e:
            if cancel and cancel.is_set():
                raise Cancelled("A műveletet a felhasználó megszakította.")
            raise ConnectionInterrupted(f"Megszakadt kapcsolat: {e}")
        latency = time.monotonic() - start
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise InvalidResponse("A válasz nem érvényes JSON.", detail=raw[:1000].decode("utf-8", "replace"))
        if not isinstance(data, dict):
            raise InvalidResponse("Váratlan JSON szerkezet.", detail=str(data)[:1000])
        if data.get("error"):
            raise ModelError(f"Modellhiba: {_error_message(json.dumps(data).encode())}")
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise InvalidResponse("A válasz nem tartalmaz 'choices' mezőt.", detail=json.dumps(data)[:1000])
        ch = choices[0]
        msg = ch.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if content is None:
            content = ch.get("text", "")
        reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "") if isinstance(msg, dict) else ""
        res = ChatResult(content=content or "", reasoning=reasoning, model=str(data.get("model") or ""),
                         finish_reason=ch.get("finish_reason"), latency=latency, streamed=False,
                         tool_calls=list(msg.get("tool_calls") or []) if isinstance(msg, dict) else [])
        _apply_usage(res, data.get("usage"), data.get("timings"))
        return res

    def _read_stream(self, resp, start: float, timeout: float,
                     on_token: Optional[TokenCallback], cancel: Optional[CancelToken]) -> ChatResult:
        parts: list[str] = []
        reasoning: list[str] = []
        model = ""
        finish = None
        usage = timings = None
        done = False
        bad = 0
        ttft = None
        tools: dict[int, dict] = {}
        try:
            for raw in resp:
                if cancel and cancel.is_set():
                    raise Cancelled("A műveletet a felhasználó megszakította.")
                if time.monotonic() - start > timeout:
                    raise LLMTimeout(f"Időtúllépés streaming közben ({timeout:.0f}s).", partial="".join(parts))
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(data_str)
                except ValueError:
                    bad += 1
                    if bad > 5:
                        raise InvalidResponse("Túl sok hibás JSON darab a streamben.", detail=data_str[:500],
                                              partial="".join(parts))
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    raise ModelError(f"Modellhiba: {_error_message(json.dumps(chunk).encode())}",
                                     partial="".join(parts))
                model = chunk.get("model") or model
                usage = chunk.get("usage") or usage
                timings = chunk.get("timings") or timings
                for ch in chunk.get("choices") or []:
                    if not isinstance(ch, dict):
                        continue
                    delta = ch.get("delta") or {}
                    piece = delta.get("content") if isinstance(delta, dict) else None
                    if piece is None:
                        piece = ch.get("text")
                    think = (delta.get("reasoning_content") or delta.get("reasoning")) if isinstance(delta, dict) else None
                    if think:
                        if ttft is None:
                            ttft = time.monotonic() - start
                        reasoning.append(think)
                        if on_token:
                            on_token(think, "reasoning")
                    if piece:
                        if ttft is None:
                            ttft = time.monotonic() - start
                        parts.append(piece)
                        if on_token:
                            on_token(piece, "content")
                    for tc in (delta.get("tool_calls") or []) if isinstance(delta, dict) else []:
                        slot = tools.setdefault(int(tc.get("index", len(tools))),
                                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            args = fn["arguments"]
                            slot["function"]["arguments"] += args if isinstance(args, str) else json.dumps(args)
                        if ttft is None:
                            ttft = time.monotonic() - start
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
        except LLMError:
            raise
        except (socket.timeout, TimeoutError):
            raise LLMTimeout("Időtúllépés: a szerver nem küldött adatot.", partial="".join(parts))
        except (ConnectionError, http.client.IncompleteRead, http.client.HTTPException, OSError, ValueError) as e:
            if cancel and cancel.is_set():
                raise Cancelled("A műveletet a felhasználó megszakította.")
            raise ConnectionInterrupted(f"A stream megszakadt: {e}", partial="".join(parts))
        if cancel and cancel.is_set():
            raise Cancelled("A műveletet a felhasználó megszakította.")
        if not done and finish is None:
            raise ConnectionInterrupted("A stream lezárás ([DONE] / finish_reason) nélkül ért véget.",
                                        partial="".join(parts))
        res = ChatResult(content="".join(parts), reasoning="".join(reasoning), model=model,
                         finish_reason=finish, latency=time.monotonic() - start, ttft=ttft, streamed=True,
                         tool_calls=[tools[k] for k in sorted(tools)])
        _apply_usage(res, usage, timings)
        return res

    def _finalize(self, res: ChatResult, payload: dict) -> ChatResult:
        content, think = split_thinking(res.content)
        if think:
            res.reasoning = (res.reasoning + "\n" + think).strip()
        res.content = content
        if not res.model:
            res.model = payload.get("model", "") or "ismeretlen"
        if not res.content.strip() and not res.tool_calls:
            hint = " (a modell csak gondolkodott – növeld a max token értéket)" if res.reasoning else ""
            raise EmptyResponse(f"A modell üres választ adott{hint}.",
                                detail="reasoning_only" if res.reasoning else None)
        if res.completion_tokens is None:
            res.completion_tokens = max(1, len(res.content + res.reasoning) // 4)
            res.tokens_estimated = True
        if res.tokens_per_second is None and res.completion_tokens and res.latency > 0:
            gen_time = res.latency - (res.ttft or 0.0)
            if gen_time > 0.05:
                res.tokens_per_second = res.completion_tokens / gen_time
        return res


def _apply_usage(res: ChatResult, usage: Any, timings: Any) -> None:
    if isinstance(usage, dict):
        res.raw_usage = usage
        res.prompt_tokens = _int(usage.get("prompt_tokens"))
        res.completion_tokens = _int(usage.get("completion_tokens"))
        res.total_tokens = _int(usage.get("total_tokens"))
    if isinstance(timings, dict):
        if res.completion_tokens is None:
            res.completion_tokens = _int(timings.get("predicted_n"))
        if res.prompt_tokens is None:
            res.prompt_tokens = _int(timings.get("prompt_n"))
        tps = timings.get("predicted_per_second")
        if isinstance(tps, (int, float)):
            res.tokens_per_second = float(tps)
    if res.total_tokens is None and res.prompt_tokens is not None and res.completion_tokens is not None:
        res.total_tokens = res.prompt_tokens + res.completion_tokens


def _int(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _abort(resp) -> None:
    """Unblock a reader thread stuck in recv() by shutting the socket down."""
    try:
        sock = resp.fp.raw._sock  # type: ignore[attr-defined]
        sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001
        pass
    _safe_close(resp)


def _safe_close(resp) -> None:
    try:
        resp.close()
    except Exception:  # noqa: BLE001
        pass
