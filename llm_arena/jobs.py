"""Background jobs with an append-only event log (streamed to the UI via SSE)."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Callable, Optional

from .providers.base import Cancelled, CancelToken, LLMError

TERMINAL = ("done", "error", "cancelled")


class Job:
    def __init__(self, kind: str, title: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.title = title
        self.status = "running"
        self.created = time.time()
        self.finished: float | None = None
        self.error: dict | None = None
        self.progress = 0.0
        self.stage = ""
        self.events: list[dict] = []
        self.cancel_token = CancelToken()
        self._cond = threading.Condition()
        self.result: dict | None = None
        self.ended = False  # True once the final job_end event is in the log

    # ------------------------------------------------------------------ events
    def emit(self, type_: str, **data) -> dict:
        with self._cond:
            evt = {"seq": len(self.events), "type": type_, "ts": time.time(), "job": self.id, **data}
            self.events.append(evt)
            if type_ == "job_end":
                self.ended = True
            if type_ == "progress":
                self.progress = float(data.get("value", self.progress))
                if data.get("label"):
                    self.stage = str(data["label"])
            self._cond.notify_all()
        return evt

    def progress_to(self, value: float, label: str = "") -> None:
        self.emit("progress", value=max(0.0, min(1.0, value)), label=label)

    def wait_events(self, since: int, timeout: float = 15.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while len(self.events) <= since and not self.ended:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self.events[since:]

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def check_cancel(self) -> None:
        self.cancel_token.raise_if_cancelled()

    def summary(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
            "created": self.created, "finished": self.finished, "error": self.error,
            "progress": self.progress, "stage": self.stage, "events": len(self.events),
        }


class JobManager:
    def __init__(self, on_finish: Optional[Callable[[Job], None]] = None, keep: int = 40):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._keep = keep
        self._on_finish = on_finish

    def start(self, kind: str, title: str, fn: Callable[[Job], Optional[dict]]) -> Job:
        job = Job(kind, title)
        with self._lock:
            self._jobs[job.id] = job
            self._prune()
        job.emit("job_start", kind=kind, title=title)

        def runner() -> None:
            try:
                job.result = fn(job) or {}
                job.status = "cancelled" if job.cancel_token.is_set() else "done"
            except Cancelled:
                job.status = "cancelled"
            except LLMError as e:
                job.status = "cancelled" if job.cancel_token.is_set() else "error"
                job.error = e.to_dict()
            except Exception as e:  # noqa: BLE001 - never kill the server
                job.status = "error"
                job.error = {"kind": "internal", "label": "Belső hiba", "message": str(e),
                             "detail": traceback.format_exc()[-3000:]}
            job.finished = time.time()
            if self._on_finish:
                try:
                    self._on_finish(job)
                except Exception:  # noqa: BLE001
                    pass
            if job.status == "done":
                job.progress = 1.0
            job.emit("job_end", status=job.status, error=job.error)

        threading.Thread(target=runner, name=f"job-{kind}-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        with self._lock:
            return [j.summary() for j in sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)]

    def running(self, kind: str | None = None) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if not j.done and (kind is None or j.kind == kind)]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.done:
            return False
        job.cancel_token.cancel()
        job.emit("log", level="warning", text="Megszakítás kérve…")
        return True

    def cancel_all(self) -> int:
        n = 0
        for j in self.running():
            j.cancel_token.cancel()
            n += 1
        return n

    def _prune(self) -> None:
        if len(self._jobs) <= self._keep:
            return
        finished = sorted((j for j in self._jobs.values() if j.done), key=lambda j: j.created)
        for j in finished[: len(self._jobs) - self._keep]:
            self._jobs.pop(j.id, None)
