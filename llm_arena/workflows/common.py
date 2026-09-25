"""Shared machinery for all workflows: calling a model with streaming events,
recording structured messages, JSON answers with repair, parallel calls."""

from __future__ import annotations

import re
import string
import threading
import time
from typing import Any, Callable

from .. import prompts
from ..jobs import Job
from ..project import ProjectStore
from ..providers import (
    Cancelled, LLMError, create_provider, fill_message, new_message,
)
from .. import attachments as att_mod
from .. import tools as tool_mod
from ..providers import EmptyResponse, ToolsUnsupported
from ..textutil import clip, extract_json

# Endpoints that rejected the native `tools` parameter (switch to the text protocol).
_NATIVE_TOOLS_UNSUPPORTED: dict[str, bool] = {}


def _rebuild_conversation(new_head: list[dict], old: list[dict]) -> list[dict]:
    """Replace the system message of ``old`` by the one in ``new_head``."""
    rest = [m for m in old if m.get("role") != "system"]
    head = [m for m in new_head if m.get("role") == "system"]
    return head + rest

SLOTS = ("A", "B")


def other(slot: str) -> str:
    return "B" if slot == "A" else "A"


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def fmt(template: str, **values: Any) -> str:
    """str.format that tolerates missing keys (renders them empty)."""
    return string.Formatter().vformat(template, (), _SafeDict(values))


class StepFailed(Exception):
    """A workflow step could not be completed (after retries)."""

    def __init__(self, stage: str, error: dict):
        super().__init__(error.get("message", "hiba"))
        self.stage = stage
        self.error = error


class WorkflowContext:
    def __init__(self, store: ProjectStore, job: Job, workflow: str):
        self.store = store
        self.job = job
        self.workflow = workflow

    # ---------------------------------------------------------------- basics
    @property
    def settings(self) -> dict:
        return self.store.settings()

    @property
    def language(self) -> str:
        return self.settings.get("language") or "magyar"

    def fmt(self, template: str, **values: Any) -> str:
        s = self.settings
        values.setdefault("language", self.language)
        values.setdefault("target", s.get("target_language") or "Python")
        return fmt(template, **values)

    def budget(self, slot: str, share: float = 1.0) -> int:
        return int(self.store.llm_config(slot).context_chars * share)

    def attachments(self, ids: list[str] | None) -> list[dict]:
        return self.store.get_attachments(ids, strict=False)

    def with_files(self, slot: str, text: str, ids: list[str] | None, share: float = 0.4,
                   images: bool = True):
        """``text`` + attached files (clipped to the model's budget); multimodal when images present."""
        recs = self.attachments(ids)
        if not recs:
            return text
        return att_mod.with_attachments(text, recs, self.budget(slot, share), images=images)

    def files_text(self, slot: str, ids: list[str] | None, share: float = 0.3) -> str:
        recs = self.attachments(ids)
        return att_mod.context_block(recs, self.budget(slot, share)) if recs else ""

    def clip_for(self, slot: str, text: str, share: float = 0.5) -> str:
        return clip(text, self.budget(slot, share))

    def check(self) -> None:
        self.job.check_cancel()

    def log(self, level: str, text: str) -> None:
        self.store.log(level, text, source=self.workflow)
        self.job.emit("log", level=level, text=text)

    def progress(self, value: float, label: str = "") -> None:
        self.job.progress_to(value, label)

    def stage(self, key: str, status: str, label: str = "", **extra: Any) -> None:
        self.job.emit("stage", stage=key, status=status, label=label, **extra)

    def state_changed(self, section: str) -> None:
        self.job.emit("state", section=section)

    # ------------------------------------------------------------------ call
    def call(self, slot: str, user: str | list[dict], *, role: str, stage: str = "", round: int = 0,
             title: str = "", system: str = "", max_tokens: int | None = None,
             temperature: float | None = None, on_created: Callable[[dict], None] | None = None,
             tools: bool = False) -> dict:
        """Call model ``slot``; stream tokens as job events; return the message dict.

        With ``tools=True`` (and web tools enabled in the settings) the model may
        call web_search / fetch_url; the calls are executed and fed back until the
        model answers (bounded by ``web_max_calls``). Native OpenAI tool calling is
        used when the server supports it, otherwise a <tool_call> text protocol.

        On failure the message is kept with ``status='error'`` and :class:`StepFailed`
        is raised (``Cancelled`` propagates unchanged).
        """
        self.check()
        cfg = self.store.llm_config(slot)
        provider = create_provider(cfg)
        settings = self.settings
        use_tools = bool(tools and settings.get("web_enabled"))
        mode = settings.get("web_tool_mode") or "auto"
        text_mode = mode == "text" or (mode == "auto" and _NATIVE_TOOLS_UNSUPPORTED.get(cfg.base_url, False))

        def build(text_mode: bool) -> list[dict]:
            parts = [cfg.system_prompt.strip(), system.strip()]
            if use_tools:
                parts.append(tool_mod.system_hint(text_mode))
            msgs: list[dict] = []
            sys_text = "\n\n".join(p for p in parts if p)
            if sys_text:
                msgs.append({"role": "system", "content": sys_text})
            if isinstance(user, str):
                msgs.append({"role": "user", "content": user})
            else:
                msgs.extend(user)
            return msgs

        messages = build(text_mode)
        msg = new_message(slot=slot, model=cfg.model if not cfg.wants_autodetect else "",
                          role=role, workflow=self.workflow, stage=stage, round=round, title=title)
        msg["prompt"] = clip(content_text(messages[-1]["content"]), 6000)
        msg["tool_calls"] = []
        with self.store.mutate() as p:
            p["messages"].append(msg)
        if on_created:
            with self.store.lock:
                on_created(msg)
        self.job.emit("msg_start", msg_id=msg["id"], slot=slot, role=role, stage=stage,
                      round=round, title=title, workflow=self.workflow)

        buf_lock = threading.Lock()
        pending: list[tuple[str, str]] = []
        last_flush = [time.monotonic()]

        def flush() -> None:
            with buf_lock:
                if not pending:
                    return
                content = "".join(d for d, k in pending if k == "content")
                reasoning = "".join(d for d, k in pending if k == "reasoning")
                pending.clear()
                last_flush[0] = time.monotonic()
            if content:
                self.job.emit("token", msg_id=msg["id"], delta=content, kind="content")
            if reasoning:
                self.job.emit("token", msg_id=msg["id"], delta=reasoning, kind="reasoning")

        def on_token(delta: str, kind: str) -> None:
            if kind == "reset":
                with buf_lock:
                    pending.clear()
                self.job.emit("msg_reset", msg_id=msg["id"])
                self.log("warning", f"LLM {slot}: újrapróbálás ({stage or role})…")
                return
            with buf_lock:
                pending.append((delta, kind))
            if time.monotonic() - last_flush[0] > 0.05:
                flush()

        overrides: dict[str, Any] = {}
        if max_tokens is not None:
            overrides["max_tokens"] = max_tokens
        if temperature is not None:
            overrides["temperature"] = temperature
        max_calls = max(0, int(settings.get("web_max_calls", 4) or 0))
        web_cfg = tool_mod.web_config(settings)
        fetch_chars = max(2000, min(12000, cfg.context_chars // 4))
        totals = {"latency": 0.0, "tokens": 0, "prompt": 0}
        calls_done = 0
        forced_final = False
        try:
            while True:
                allow = use_tools and calls_done < max_calls and not forced_final
                ov = dict(overrides)
                if allow and not text_mode:
                    ov["tools"] = tool_mod.TOOL_SPECS
                try:
                    result = provider.chat(messages, on_token=on_token, cancel=self.job.cancel_token, **ov)
                except ToolsUnsupported as e:
                    # Remember and switch to the text protocol for this endpoint.
                    _NATIVE_TOOLS_UNSUPPORTED[cfg.base_url] = True
                    self.log("warning", f"LLM {slot}: natív tool-hívás nem támogatott ({e.message}) – "
                                        "szöveges eszközprotokollra váltás.")
                    text_mode = True
                    messages = _rebuild_conversation(build(True), messages)
                    continue
                flush()
                totals["latency"] += result.latency
                totals["tokens"] += result.completion_tokens or 0
                totals["prompt"] += result.prompt_tokens or 0
                calls = tool_mod.normalize_native(result.tool_calls) if use_tools else []
                if use_tools:
                    text_calls, cleaned = tool_mod.parse_text_calls(result.content)
                    if text_calls or cleaned != result.content:
                        result.content = cleaned
                    calls += text_calls
                if not calls:
                    if not result.content.strip():
                        if use_tools and not forced_final:
                            forced_final = True
                            messages = messages + [{"role": "user", "content":
                                                    "Give your final answer now, without calling tools."}]
                            continue
                        raise EmptyResponse("A modell üres választ adott.")
                    break
                if not allow:
                    # Tool budget exhausted but the model still wants tools: force a final answer.
                    forced_final = True
                    messages = messages + [{"role": "assistant", "content": result.content or "(tool call)"},
                                           {"role": "user", "content": "Tool budget exhausted. Give your final "
                                                                       "answer now using the information you have."}]
                    self.job.emit("msg_reset", msg_id=msg["id"])
                    continue
                calls = calls[: max(1, max_calls - calls_done)]
                results = []
                for c in calls:
                    self.check()
                    rec = {"id": c["id"], "name": c["name"], "arguments": c["arguments"], "status": "running",
                           "summary": "", "sources": [], "started": time.time()}
                    with self.store.mutate():
                        msg["tool_calls"].append(rec)
                    self.job.emit("tool", msg_id=msg["id"], call=dict(rec))
                    label = c["arguments"].get("query") or c["arguments"].get("url") or ""
                    self.log("info", f"LLM {slot} eszközhívás: {c['name']}({label})")
                    r = tool_mod.execute(c["name"], c["arguments"], web_cfg, max_chars=fetch_chars)
                    with self.store.mutate():
                        rec.update(status="done" if r["ok"] else "error", summary=r["summary"],
                                   sources=r["sources"], duration=r["duration"], error=r.get("error"))
                    self.job.emit("tool", msg_id=msg["id"], call=dict(rec))
                    results.append(r)
                    calls_done += 1
                messages = messages + tool_mod.as_tool_messages(calls, results, not text_mode, result.content)
                self.job.emit("msg_reset", msg_id=msg["id"])
        except Cancelled as e:
            flush()
            with self.store.mutate():
                msg.update(status="cancelled", error=e.to_dict(), content=e.partial or msg["content"])
            self.job.emit("msg_error", msg_id=msg["id"], error=e.to_dict())
            raise
        except LLMError as e:
            flush()
            err = e.to_dict()
            if any(isinstance(m.get("content"), list) for m in messages) and re.search(
                    r"image|multimodal|mmproj|vision|image_url", f"{err.get('message')} {err.get('detail')}", re.I):
                err["label"] = "A modell nem tud képet feldolgozni"
                err["hint"] = ("A csatolt kép miatt hibázott: ez a modell / szerver nem multimodális. Indítsd a "
                               "llama-servert --mmproj fájllal (képet értő modellel), vagy távolítsd el a képet.")
            with self.store.mutate():
                msg.update(status="error", error=err, content=e.partial or "")
            self.job.emit("msg_error", msg_id=msg["id"], error=err)
            self.log("error", f"LLM {slot} ({cfg.name}) – {stage or role}: {err['label']}: {err['message']}")
            raise StepFailed(stage or role, err) from e
        with self.store.mutate():
            fill_message(msg, result)
            if msg["tool_calls"]:
                msg["latency"] = round(totals["latency"], 3)
                msg["tokens"] = totals["tokens"] or msg["tokens"]
                msg["prompt_tokens"] = totals["prompt"] or msg["prompt_tokens"]
        self.job.emit("msg_end", message=dict(msg))
        tok = f"{msg['tokens']}{'~' if msg['tokens_estimated'] else ''} token"
        extra = f", {len(msg['tool_calls'])} eszközhívás" if msg["tool_calls"] else ""
        self.store.log("info", f"LLM {slot} ({msg['model']}) – {title or stage or role}: {msg['latency']:.1f}s, "
                               f"{tok}{extra}", source=self.workflow)
        return msg

    def call_json(self, slot: str, user: str, *, expect: type = dict, **kw: Any) -> tuple[dict, Any]:
        """Call and parse a JSON answer; one repair round-trip when parsing fails."""
        msg = self.call(slot, user, **kw)
        data = extract_json(msg["content"], expect=expect)
        if data is None and self.settings.get("json_repair", True):
            self.log("warning", f"LLM {slot}: a JSON nem értelmezhető, javítás kérése…")
            repair = self.fmt(prompts.JSON_REPAIR, answer=clip(msg["content"], 8000))
            kw2 = dict(kw)
            kw2["title"] = (kw.get("title") or "") + " (JSON javítás)"
            try:
                msg2 = self.call(slot, repair, **kw2)
                data = extract_json(msg2["content"], expect=expect)
                if data is not None:
                    msg = msg2
            except StepFailed:
                pass
        with self.store.mutate():
            msg["structured"] = data
        return msg, data

    def parallel(self, calls: dict[str, Callable[[], Any]]) -> dict[str, tuple[Any, BaseException | None]]:
        """Run callables concurrently (one per model). Errors are returned, not raised."""
        results: dict[str, tuple[Any, BaseException | None]] = {}

        def run(key: str, fn: Callable[[], Any]) -> None:
            try:
                results[key] = (fn(), None)
            except BaseException as e:  # noqa: BLE001
                results[key] = (None, e)

        threads = [threading.Thread(target=run, args=(k, f), daemon=True) for k, f in calls.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if any(isinstance(e, Cancelled) for _, e in results.values()) or self.job.cancel_token.is_set():
            raise Cancelled("A műveletet a felhasználó megszakította.")
        return results


def content_text(content) -> str:
    """Text of a chat message content (plain string or multimodal parts)."""
    if isinstance(content, list):
        return "\n".join(p.get("text", "[kép]") if isinstance(p, dict) else str(p) for p in content)
    return content or ""


def message_text(store: ProjectStore, msg_id: str | None) -> str:
    if not msg_id:
        return ""
    m = store.find_message(msg_id)
    return (m or {}).get("content", "") if m and m.get("status") == "done" else ""
