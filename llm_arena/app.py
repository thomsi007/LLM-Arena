"""Application service layer: glues store, jobs, providers and workflows.

The HTTP server (``server.py``) only translates requests into calls on
:class:`ArenaApp`, so another front-end (CLI, desktop) could reuse it.
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from typing import Any, Callable

from .jobs import Job, JobManager
from .project import ProjectStore
from .providers import LLMConfig, LLMError, create_provider
from .workflows import arena, consensus, debate, design, pipeline, testing
from .workflows.common import SLOTS, WorkflowContext, message_text

# Workflows that must not run twice at the same time (they share a state section).
EXCLUSIVE = {"debate": "debate", "design": "design", "pipeline": "pipeline", "testing": "testing"}


class ConflictError(Exception):
    pass


class ArenaApp:
    def __init__(self, data_dir: str = "data", autosave_interval: float = 10.0):
        self.store = ProjectStore(data_dir)
        self.jobs = JobManager(on_finish=self._on_job_finish)
        self._stop = threading.Event()
        if autosave_interval > 0:
            threading.Thread(target=self._autosave_loop, args=(autosave_interval,), daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        self.jobs.cancel_all()
        self.store.autosave()

    def _autosave_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            self.store.autosave()

    def _on_job_finish(self, job: Job) -> None:
        level = {"done": "info", "cancelled": "warning"}.get(job.status, "error")
        msg = f"Feladat vége: {job.title} – {job.status}"
        if job.error:
            msg += f" ({job.error.get('label', '')}: {job.error.get('message', '')})"
        self.store.log(level, msg, source=job.kind)
        self.store.autosave()

    # ------------------------------------------------------------------ jobs
    def _start(self, kind: str, title: str, fn: Callable[[WorkflowContext], Any]) -> Job:
        section = EXCLUSIVE.get(kind)
        if section:
            busy = [j for j in self.jobs.running() if EXCLUSIVE.get(j.kind) == section
                    or j.kind == "pipeline" or kind == "pipeline"]
            if busy:
                raise ConflictError(f"Már fut egy ütköző folyamat: {busy[0].title}")
        self.store.log("info", f"Feladat indul: {title}", source=kind)

        def run(job: Job) -> dict:
            ctx = WorkflowContext(self.store, job, kind)
            result = fn(ctx)
            return result if isinstance(result, dict) else {"ok": True}

        return self.jobs.start(kind, title, run)

    def start_arena(self, prompt: str, multi_turn: bool = True) -> Job:
        return self._start("arena", "Aréna kör", lambda ctx: arena.run_arena(ctx, prompt, multi_turn=multi_turn))

    def retry_arena(self, round_no: int, slot: str) -> Job:
        _check_slot(slot)
        return self._start("arena", f"Aréna újrapróbálás ({slot}, {round_no}. kör)",
                           lambda ctx: arena.retry_slot(ctx, round_no, slot))

    def start_consensus(self, source: str = "arena", round_no: int | None = None,
                        candidates: dict | None = None, task: str = "") -> Job:
        snap = self.store.snapshot()
        if candidates is None:
            rounds = snap["arena"]["rounds"]
            if not rounds:
                raise ValueError("Nincs aréna kör, amiről dönteni lehetne.")
            rnd = next((r for r in rounds if r["round"] == round_no), rounds[-1]) if round_no else rounds[-1]
            candidates = {s: message_text(self.store, rnd["responses"].get(s)) for s in SLOTS}
            task = task or rnd["prompt"]
            source = f"arena:{rnd['round']}"
        missing = [s for s in SLOTS if not (candidates.get(s) or "").strip()]
        if missing:
            raise ValueError(f"Hiányzó sikeres válasz: LLM {', '.join(missing)}")
        return self._start("consensus", "Közös döntés",
                           lambda ctx: consensus.run_consensus(ctx, task=task or snap["task"],
                                                               candidates=candidates, source=source))

    def start_debate(self, topic: str = "", rounds: int | None = None, resume: bool = False) -> Job:
        return self._start("debate", "Vita" + (" (folytatás)" if resume else ""),
                           lambda ctx: debate.run_debate(ctx, topic=topic, rounds=rounds, resume=resume))

    def start_design(self, requirements: str = "", developer: str | None = None, resume: bool = False,
                     run_tests: bool = True) -> Job:
        if developer:
            _check_slot(developer)
        return self._start("design", "Közös programtervezés" + (" (folytatás)" if resume else ""),
                           lambda ctx: design.run_design(ctx, requirements=requirements, developer=developer,
                                                         resume=resume, run_tests=run_tests))

    def start_testing(self, action: str, max_iterations: int | None = None) -> Job:
        req = self._requirements()
        if action == "generate":
            fn = lambda ctx: {"files": sorted(testing.generate_tests(ctx, req))}  # noqa: E731
            title = "Tesztek generálása"
        elif action == "run":
            def fn(ctx):
                run = testing.run_suite(ctx, 0, "Tesztek futtatása")
                return testing.build_report(ctx, run, 0, 0)
            title = "Tesztek futtatása"
        elif action == "loop":
            fn = lambda ctx: testing.fix_loop(ctx, req, max_iterations)  # noqa: E731
            title = "Teszt → javítás ciklus"
        else:
            raise ValueError("Ismeretlen tesztművelet.")
        return self._start("testing", title, fn)

    def start_pipeline(self, task: str = "", resume: bool = False) -> Job:
        return self._start("pipeline", "Teljes folyamat" + (" (folytatás)" if resume else ""),
                           lambda ctx: pipeline.run_pipeline(ctx, task=task, resume=resume))

    def _requirements(self) -> str:
        snap = self.store.snapshot()
        return (snap.get("design") or {}).get("requirements") or snap.get("task") or \
            "(nincs követelmény megadva – a kód viselkedéséből indulj ki)"

    # ------------------------------------------------------------ LLM config
    def update_config(self, data: dict) -> dict:
        with self.store.mutate() as p:
            for slot in SLOTS:
                if slot in (data.get("llms") or {}):
                    incoming = dict(data["llms"][slot])
                    if incoming.get("api_key") == "***":  # masked value from the UI
                        incoming["api_key"] = p["llms"][slot].get("api_key", "")
                    merged = {**p["llms"][slot], **incoming}
                    p["llms"][slot] = LLMConfig.from_dict(merged, slot).to_dict()
            if isinstance(data.get("settings"), dict):
                s = p["settings"]
                for k, v in data["settings"].items():
                    if k in s and not (k == "web_brave_api_key" and v == "***"):
                        s[k] = _coerce(v, s[k])
                s["max_fix_iterations"] = max(0, min(int(s["max_fix_iterations"]), 10))
                s["debate_rounds"] = max(1, min(int(s["debate_rounds"]), 8))
                s["test_timeout"] = max(5, min(int(s["test_timeout"]), 600))
                s["web_max_calls"] = max(0, min(int(s["web_max_calls"]), 12))
                s["web_max_results"] = max(1, min(int(s["web_max_results"]), 15))
                if s["web_backend"] not in ("duckduckgo", "searxng", "brave"):
                    s["web_backend"] = "duckduckgo"
                if s["web_tool_mode"] not in ("auto", "native", "text"):
                    s["web_tool_mode"] = "auto"
                for k in ("developer", "moderator"):
                    if s[k] not in SLOTS:
                        s[k] = "A"
            if data.get("task") is not None:
                p["task"] = str(data["task"])
            if data.get("name"):
                p["name"] = str(data["name"])[:200]
        self.store.save_defaults()
        return self.public_config()

    def public_config(self) -> dict:
        snap = self.store.snapshot()
        return {"llms": {s: LLMConfig.from_dict(snap["llms"][s], s).to_dict(include_secret=False) for s in SLOTS},
                "settings": _mask_settings(snap["settings"]), "name": snap["name"], "task": snap["task"], "id": snap["id"]}

    def _provider(self, slot: str, override: dict | None = None):
        _check_slot(slot)
        cfg = self.store.llm_config(slot)
        if override:
            o = dict(override)
            if o.get("api_key") == "***":
                o.pop("api_key")
            cfg = LLMConfig.from_dict({**cfg.to_dict(), **o}, slot)
        return create_provider(cfg)

    def test_connection(self, slot: str, override: dict | None = None, probe: bool = True) -> dict:
        prov = self._provider(slot, override)
        res = prov.test_connection(probe_completion=probe)
        level = "info" if res["ok"] else "error"
        self.store.log(level, f"Kapcsolatteszt LLM {slot} ({prov.config.base_url}): "
                              f"{'OK' if res['ok'] else 'HIBA'} – modell: {res.get('model') or '?'}", source="config")
        return res

    def detect_models(self, slot: str, override: dict | None = None) -> dict:
        prov = self._provider(slot, override)
        try:
            models = prov.list_models()
            return {"ok": True, "models": models, "model": models[0] if models else ""}
        except LLMError as e:
            return {"ok": False, "models": [], "error": e.to_dict()}

    # --------------------------------------------------------------- exports
    def conversation_markdown(self) -> str:
        snap = self.store.snapshot()
        out = [f"# {snap['name']}", "", f"**Feladat:** {snap['task'] or '-'}", ""]
        for slot in SLOTS:
            c = snap["llms"][slot]
            out.append(f"- **LLM {slot}** ({c['name']}): `{c['base_url']}` modell: `{c['model'] or 'auto'}`, "
                       f"temperature {c['temperature']}, max token {c['max_tokens']}")
        out.append("")
        for m in snap["messages"]:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["created"]))
            out.append(f"## LLM {m['slot']} · {m['workflow']} · {m['title'] or m['stage']}"
                       + (f" · {m['round']}. kör" if m.get("round") else ""))
            meta = f"*{ts} · modell: {m['model'] or '?'} · szerep: {m['role']} · {m['latency']}s · {m['tokens']} token*"
            out.append(meta)
            if m.get("error"):
                out.append(f"> **Hiba:** {m['error'].get('label')}: {m['error'].get('message')}")
            if m.get("prompt"):
                out.append("<details><summary>Prompt</summary>\n\n```\n" + m["prompt"] + "\n```\n</details>")
            out += ["", m.get("content") or "", ""]
        syn = (snap.get("debate") or {}).get("synthesis")
        if syn:
            out += ["# Vita összegzése", "", debate.synthesis_text(snap["debate"]), ""]
        if snap.get("final") and snap["final"].get("report"):
            out += ["# Végleges jelentés", "", snap["final"]["report"]]
        return "\n".join(out)

    def conversation_json(self) -> dict:
        snap = self.store.snapshot()
        return {"name": snap["name"], "task": snap["task"],
                "messages": [{k: m.get(k) for k in ("id", "model", "slot", "role", "workflow", "stage", "round",
                                                    "title", "content", "latency", "tokens", "prompt_tokens",
                                                    "tokens_estimated", "error", "created", "structured")}
                             for m in snap["messages"]]}

    def code_zip(self, version: int | None = None) -> bytes:
        snap = self.store.snapshot()
        files = snap["code"]["files"]
        if version:
            v = next((v for v in snap["code"]["versions"] if v["version"] == version), None)
            if not v:
                raise ValueError("Nincs ilyen verzió.")
            files = v["files"]
        if not files:
            raise ValueError("Még nincs generált kód.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, content in sorted(files.items()):
                z.writestr(name, content)
            report = snap["testing"].get("report")
            if report:
                z.writestr("_arena/test_report.json", json.dumps(report, ensure_ascii=False, indent=1))
        return buf.getvalue()

    def edit_code(self, files: dict, deleted: list | None = None, note: str = "") -> dict:
        from .textutil import safe_path
        clean = {}
        for name, content in (files or {}).items():
            p = safe_path(name)
            if not p:
                raise ValueError(f"Érvénytelen fájlnév: {name}")
            clean[p] = str(content)
        with self.store.lock:
            current = dict(self.store.project["code"]["files"])
        current.update(clean)
        for d in deleted or []:
            current.pop(d, None)
        v = self.store.set_code(current, source="manual", note=note or "Kézi módosítás", replace=True)
        return {"version": v["version"]}


def _mask_settings(settings: dict) -> dict:
    s = dict(settings)
    if s.get("web_brave_api_key"):
        s["web_brave_api_key"] = "***"
    return s


def _check_slot(slot: str) -> None:
    if slot not in SLOTS:
        raise ValueError("Érvénytelen modell (A vagy B).")


def _coerce(value: Any, like: Any) -> Any:
    try:
        if isinstance(like, bool):
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        if isinstance(like, int):
            return int(float(value))
        if isinstance(like, float):
            return float(value)
        return str(value)
    except (TypeError, ValueError):
        return like
