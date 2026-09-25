"""Tiny JSON + SSE HTTP server (standard library) exposing :class:`ArenaApp`."""

from __future__ import annotations

import json
import mimetypes
import re
import socket
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .app import ArenaApp, ConflictError

STATIC_DIR = Path(__file__).parent / "static"
# Explicit types: on Windows `mimetypes` reads the registry, which often maps
# .js to text/plain – browsers then refuse to execute the ES modules.
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
MAX_BODY = 50 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn.__name__))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = "LLMArena/1.0"
    protocol_version = "HTTP/1.1"
    app: ArenaApp  # injected

    # ----------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):  # quieter console
        if "/api/jobs/" in (self.path or "") and "/events" in self.path:
            return
        super().log_message(fmt, *args)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        self.query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(404, "Nem található")
                return self._static(path)
            for m, rx, name in ROUTES:
                if m == method:
                    match = rx.match(path)
                    if match:
                        return getattr(self, name)(**match.groupdict())
            raise ApiError(404, f"Ismeretlen végpont: {method} {path}")
        except ApiError as e:
            self._json({"ok": False, "error": e.message}, e.status)
        except ConflictError as e:
            self._json({"ok": False, "error": str(e)}, 409)
        except (ValueError, KeyError, TypeError) as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.app.store.log("error", f"Szerverhiba: {e}", source="server")
            try:
                self._json({"ok": False, "error": f"Belső hiba: {e}"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "Túl nagy kérés.")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "Hibás JSON a kérésben.")
        if not isinstance(data, dict):
            raise ApiError(400, "JSON objektum szükséges.")
        return data

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _download(self, body: bytes, filename: str, ctype: str) -> None:
        self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{filename}"'})

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve() or not target.is_file():
            raise ApiError(404, "Nem található")
        ctype = STATIC_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] \
            or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def _job(self, job) -> None:
        self._json({"ok": True, "job": job.summary()})

    # ------------------------------------------------------------- config
    @route("GET", "/api/health")
    def api_health(self):
        self._json({"ok": True, "time": time.time()})

    @route("GET", "/api/config")
    def api_config(self):
        self._json({"ok": True, **self.app.public_config()})

    @route("POST", "/api/config")
    def api_config_update(self):
        self._json({"ok": True, **self.app.update_config(self._body())})

    @route("POST", "/api/llm/(?P<slot>[AB])/test")
    def api_llm_test(self, slot):
        body = self._body()
        self._json({"ok": True, "result": self.app.test_connection(slot, body.get("config"),
                                                                   probe=body.get("probe", True))})

    @route("POST", "/api/web/test")
    def api_web_test(self):
        from . import tools
        b = self._body()
        cfg = tools.web_config(self.app.store.settings())
        r = tools.execute("web_search", {"query": b.get("query") or "llama.cpp latest release"}, cfg)
        self.app.store.log("info" if r["ok"] else "error", f"Webes keresés teszt: {r['summary']}", source="web")
        self._json({"ok": True, "result": r})

    @route("POST", "/api/llm/(?P<slot>[AB])/models")
    def api_llm_models(self, slot):
        self._json({"ok": True, **self.app.detect_models(slot, self._body().get("config"))})

    # ---------------------------------------------------------- workflows
    @route("POST", "/api/arena/run")
    def api_arena(self):
        b = self._body()
        self._job(self.app.start_arena(b.get("prompt", ""), bool(b.get("multi_turn", True))))

    @route("POST", "/api/arena/retry")
    def api_arena_retry(self):
        b = self._body()
        self._job(self.app.retry_arena(int(b["round"]), str(b["slot"])))

    @route("POST", "/api/arena/clear")
    def api_arena_clear(self):
        with self.app.store.mutate() as p:
            p["arena"]["rounds"] = []
        self._json({"ok": True})

    @route("POST", "/api/consensus/run")
    def api_consensus(self):
        b = self._body()
        rnd = b.get("round")
        self._job(self.app.start_consensus(b.get("source", "arena"), int(rnd) if rnd else None,
                                           b.get("candidates"), b.get("task", "")))

    @route("POST", "/api/debate/start")
    def api_debate(self):
        b = self._body()
        rounds = b.get("rounds")
        self._job(self.app.start_debate(b.get("topic", ""), int(rounds) if rounds else None,
                                        bool(b.get("resume"))))

    @route("POST", "/api/design/start")
    def api_design(self):
        b = self._body()
        self._job(self.app.start_design(b.get("requirements", ""), b.get("developer"), bool(b.get("resume")),
                                        bool(b.get("run_tests", True))))

    @route("POST", "/api/testing/(?P<action>generate|run|loop)")
    def api_testing(self, action):
        b = self._body()
        mi = b.get("max_iterations")
        self._job(self.app.start_testing(action, int(mi) if mi not in (None, "") else None))

    @route("POST", "/api/pipeline/start")
    def api_pipeline(self):
        b = self._body()
        self._job(self.app.start_pipeline(b.get("task", ""), bool(b.get("resume"))))

    # --------------------------------------------------------------- jobs
    @route("GET", "/api/jobs")
    def api_jobs(self):
        self._json({"ok": True, "jobs": self.app.jobs.list()})

    @route("POST", "/api/jobs/(?P<job_id>\\w+)/cancel")
    def api_job_cancel(self, job_id):
        self._json({"ok": self.app.jobs.cancel(job_id)})

    @route("POST", "/api/jobs/cancel-all")
    def api_job_cancel_all(self):
        self._json({"ok": True, "cancelled": self.app.jobs.cancel_all()})

    @route("GET", "/api/jobs/(?P<job_id>\\w+)/events")
    def api_job_events(self, job_id):
        job = self.app.jobs.get(job_id)
        if not job:
            raise ApiError(404, "Ismeretlen feladat.")
        since = int(self.query.get("since") or self.headers.get("Last-Event-ID", -1) or -1) + 1
        since = max(0, since)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                events = job.wait_events(since, timeout=15.0)
                if not events:
                    if job.ended:
                        break
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                buf = []
                for evt in events:
                    buf.append(f"id: {evt['seq']}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n")
                self.wfile.write("".join(buf).encode("utf-8"))
                self.wfile.flush()
                since = events[-1]["seq"] + 1
                if job.ended and since >= len(job.events):
                    break
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return

    # ------------------------------------------------------------ project
    @route("GET", "/api/project")
    def api_project(self):
        data = self.app.store.snapshot()
        for cfg in data["llms"].values():
            cfg["api_key"] = "***" if cfg.get("api_key") else ""
        if data["settings"].get("web_brave_api_key"):
            data["settings"]["web_brave_api_key"] = "***"
        self._json({"ok": True, "project": data, "jobs": self.app.jobs.list()})

    @route("POST", "/api/project/new")
    def api_project_new(self):
        if self.app.jobs.running():
            raise ApiError(409, "Előbb állítsd le a futó folyamatokat.")
        b = self._body()
        self.app.store.autosave()
        p = self.app.store.new(b.get("name", ""), keep_settings=b.get("keep_settings", True))
        self._json({"ok": True, "id": p["id"]})

    @route("POST", "/api/project/save")
    def api_project_save(self):
        b = self._body()
        if b.get("name"):
            with self.app.store.mutate() as p:
                p["name"] = str(b["name"])[:200]
        path = self.app.store.save()
        self.app.store.log("info", f"Projekt mentve: {path}", source="project")
        self._json({"ok": True, "path": path})

    @route("GET", "/api/projects")
    def api_projects(self):
        self._json({"ok": True, "projects": self.app.store.list_saved()})

    @route("POST", "/api/project/load")
    def api_project_load(self):
        if self.app.jobs.running():
            raise ApiError(409, "Előbb állítsd le a futó folyamatokat.")
        self.app.store.autosave()
        try:
            p = self.app.store.load(self._body().get("id", ""))
        except FileNotFoundError:
            raise ApiError(404, "A projekt nem található.")
        self.app.store.log("info", f"Projekt betöltve: {p['name']}", source="project")
        self._json({"ok": True, "id": p["id"]})

    @route("POST", "/api/project/delete")
    def api_project_delete(self):
        self._json({"ok": self.app.store.delete_saved(self._body().get("id", ""))})

    @route("GET", "/api/project/export")
    def api_project_export(self):
        data = self.app.store.export(include_keys=self.query.get("include_keys") == "1")
        name = _fname(data["name"]) + ".arena.json"
        self._download(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"), name, "application/json")

    @route("POST", "/api/project/import")
    def api_project_import(self):
        if self.app.jobs.running():
            raise ApiError(409, "Előbb állítsd le a futó folyamatokat.")
        p = self.app.store.import_project(self._body())
        self.app.store.log("info", f"Projekt importálva: {p['name']}", source="project")
        self._json({"ok": True, "id": p["id"]})

    @route("GET", "/api/export/conversation")
    def api_export_conv(self):
        name = _fname(self.app.store.project["name"])
        if self.query.get("format") == "json":
            body = json.dumps(self.app.conversation_json(), ensure_ascii=False, indent=1).encode("utf-8")
            self._download(body, name + ".conversation.json", "application/json")
        else:
            self._download(self.app.conversation_markdown().encode("utf-8"), name + ".md", "text/markdown; charset=utf-8")

    @route("GET", "/api/export/html")
    def api_export_html(self):
        from . import report
        section = self.query.get("section", "all")
        body = report.render(self.app.store.snapshot(), section).encode("utf-8")
        name = f"{_fname(self.app.store.project['name'])}_{section}.html"
        self._download(body, name, "text/html; charset=utf-8")

    @route("GET", "/api/code/download")
    def api_code_download(self):
        v = self.query.get("version")
        body = self.app.code_zip(int(v) if v else None)
        self._download(body, _fname(self.app.store.project["name"]) + "_code.zip", "application/zip")

    @route("POST", "/api/code/edit")
    def api_code_edit(self):
        b = self._body()
        self._json({"ok": True, **self.app.edit_code(b.get("files") or {}, b.get("deleted"), b.get("note", ""))})

    @route("GET", "/api/log")
    def api_log(self):
        since = float(self.query.get("since") or 0)
        with self.app.store.lock:
            entries = [e for e in self.app.store.project["log"] if e["ts"] > since]
        self._json({"ok": True, "log": entries[-1000:]})

    @route("POST", "/api/log/clear")
    def api_log_clear(self):
        with self.app.store.mutate() as p:
            p["log"] = []
        self._json({"ok": True})


def _fname(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name or "project").strip("_")[:60] or "project"


class ArenaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(app: ArenaApp, host: str = "127.0.0.1", port: int = 8765) -> ArenaHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    return ArenaHTTPServer((host, port), handler)
