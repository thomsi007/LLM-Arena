"""Shared machinery for all workflows: calling a model with streaming events,
recording structured messages, JSON answers with repair, parallel calls."""

from __future__ import annotations

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
from ..textutil import clip, extract_json

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
             temperature: float | None = None, on_created: Callable[[dict], None] | None = None) -> dict:
        """Call model ``slot``; stream tokens as job events; return the message dict.

        On failure the message is kept with ``status='error'`` and :class:`StepFailed`
        is raised (``Cancelled`` propagates unchanged).
        """
        self.check()
        cfg = self.store.llm_config(slot)
        provider = create_provider(cfg)
        sys_parts = [p for p in (cfg.system_prompt.strip(), system.strip()) if p]
        messages: list[dict] = []
        if sys_parts:
            messages.append({"role": "system", "content": "\n\n".join(sys_parts)})
        if isinstance(user, str):
            messages.append({"role": "user", "content": user})
        else:
            messages.extend(user)

        msg = new_message(slot=slot, model=cfg.model if not cfg.wants_autodetect else "",
                          role=role, workflow=self.workflow, stage=stage, round=round, title=title)
        msg["prompt"] = clip(messages[-1]["content"], 6000)
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
        try:
            result = provider.chat(messages, on_token=on_token, cancel=self.job.cancel_token, **overrides)
            flush()
        except Cancelled as e:
            flush()
            with self.store.mutate():
                msg.update(status="cancelled", error=e.to_dict(), content=e.partial or msg["content"])
            self.job.emit("msg_error", msg_id=msg["id"], error=e.to_dict())
            raise
        except LLMError as e:
            flush()
            err = e.to_dict()
            with self.store.mutate():
                msg.update(status="error", error=err, content=e.partial or "")
            self.job.emit("msg_error", msg_id=msg["id"], error=err)
            self.log("error", f"LLM {slot} ({cfg.name}) – {stage or role}: {err['label']}: {err['message']}")
            raise StepFailed(stage or role, err) from e
        with self.store.mutate():
            fill_message(msg, result)
        self.job.emit("msg_end", message=dict(msg))
        tok = f"{msg['tokens']}{'~' if msg['tokens_estimated'] else ''} token"
        self.store.log("info", f"LLM {slot} ({msg['model']}) – {title or stage or role}: {msg['latency']:.1f}s, {tok}",
                       source=self.workflow)
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


def message_text(store: ProjectStore, msg_id: str | None) -> str:
    if not msg_id:
        return ""
    m = store.find_message(msg_id)
    return (m or {}).get("content", "") if m and m.get("status") == "done" else ""
