"""Project / session state: one JSON document holding everything a run produces.

The store is the single source of truth. Workflows mutate it only through the
helpers below (which take the lock), the UI reads snapshots of it, and it can
be saved, exported and re-imported at any time.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from .providers.base import LLMConfig

SCHEMA_VERSION = 1
MAX_LOG = 3000


def default_settings() -> dict:
    return {
        "language": "magyar",
        "allow_code_execution": False,
        "test_timeout": 60,
        "max_fix_iterations": 3,
        "debate_rounds": 2,
        "json_repair": True,
        "developer": "A",
        "moderator": "A",
        "dual_architecture": True,
        "target_language": "Python",
    }


def default_llms() -> dict:
    return {
        "A": LLMConfig.from_dict({"base_url": "http://127.0.0.1:8080", "name": "LLM A"}, "A").to_dict(),
        "B": LLMConfig.from_dict({"base_url": "http://127.0.0.1:8081", "name": "LLM B"}, "B").to_dict(),
    }


def new_project(name: str = "", llms: dict | None = None, settings: dict | None = None) -> dict:
    now = time.time()
    return {
        "schema_version": SCHEMA_VERSION,
        "id": uuid.uuid4().hex[:12],
        "name": name or time.strftime("Projekt %Y-%m-%d %H:%M"),
        "created": now,
        "updated": now,
        "task": "",
        "settings": {**default_settings(), **(settings or {})},
        "llms": copy.deepcopy(llms) if llms else default_llms(),
        "messages": [],
        "arena": {"rounds": []},
        "debate": {},
        "design": {},
        "code": {"files": {}, "versions": []},
        "testing": {"runs": [], "iterations": [], "fixed_bugs": [], "report": None},
        "consensus": [],
        "pipeline": {},
        "final": None,
        "changes": [],
        "log": [],
    }


def migrate(data: dict) -> dict:
    """Fill in keys missing from older / hand-edited exports."""
    if not isinstance(data, dict):
        raise ValueError("A projekt fájl nem JSON objektum.")
    base = new_project()
    for key, value in base.items():
        if key not in data or data[key] is None and value is not None:
            data[key] = value
    data["settings"] = {**default_settings(), **(data.get("settings") or {})}
    llms = data.get("llms") or {}
    data["llms"] = {s: LLMConfig.from_dict(llms.get(s), s).to_dict() for s in ("A", "B")}
    for k, v in (("files", {}), ("versions", [])):
        data["code"].setdefault(k, v)
    for k in ("runs", "iterations", "fixed_bugs"):
        data["testing"].setdefault(k, [])
    data["schema_version"] = SCHEMA_VERSION
    return data


class ProjectStore:
    """Thread-safe holder of the current project + on-disk persistence."""

    def __init__(self, data_dir: str | os.PathLike):
        self.data_dir = Path(data_dir)
        self.projects_dir = self.data_dir / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.data_dir / "settings.json"
        self.lock = threading.RLock()
        self._listeners: list[Callable[[dict], None]] = []
        defaults = self._load_defaults()
        self.project = new_project(llms=defaults.get("llms"), settings=defaults.get("settings"))
        self._dirty = False

    # ---------------------------------------------------------------- defaults
    def _load_defaults(self) -> dict:
        try:
            return json.loads(self.settings_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def save_defaults(self) -> None:
        with self.lock:
            data = {"llms": self.project["llms"], "settings": self.project["settings"]}
        _atomic_write(self.settings_file, json.dumps(data, ensure_ascii=False, indent=1))

    # -------------------------------------------------------------- accessors
    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.project)

    def llm_config(self, slot: str) -> LLMConfig:
        with self.lock:
            return LLMConfig.from_dict(self.project["llms"].get(slot), slot)

    def settings(self) -> dict:
        with self.lock:
            return dict(self.project["settings"])

    def mutate(self) -> "_Mutation":
        return _Mutation(self)

    def touch(self) -> None:
        self.project["updated"] = time.time()
        self._dirty = True

    def find_message(self, msg_id: str) -> dict | None:
        with self.lock:
            for m in reversed(self.project["messages"]):
                if m["id"] == msg_id:
                    return m
        return None

    def iter_messages(self, ids: list[str]) -> Iterator[dict]:
        with self.lock:
            index = {m["id"]: m for m in self.project["messages"]}
            for i in ids:
                if i in index:
                    yield copy.deepcopy(index[i])

    # --------------------------------------------------------------- logging
    def log(self, level: str, text: str, source: str = "app", **extra: Any) -> dict:
        entry = {"ts": time.time(), "level": level, "source": source, "text": text, **extra}
        with self.lock:
            log = self.project["log"]
            log.append(entry)
            if len(log) > MAX_LOG:
                del log[: len(log) - MAX_LOG]
            self.touch()
        for cb in list(self._listeners):
            try:
                cb(entry)
            except Exception:  # noqa: BLE001
                pass
        return entry

    def add_log_listener(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def record_change(self, kind: str, description: str, **extra: Any) -> None:
        with self.lock:
            self.project["changes"].append({"ts": time.time(), "type": kind, "description": description, **extra})
            self.touch()

    # ------------------------------------------------------------------ code
    def set_code(self, files: dict[str, str], *, source: str, note: str = "", replace: bool = False) -> dict:
        """Apply a set of files, creating a new version with a diff to the previous one."""
        with self.lock:
            code = self.project["code"]
            before = dict(code["files"])
            after = dict(files) if replace else {**before, **files}
            if after == before and code["versions"]:
                return code["versions"][-1]
            diff = unified_diff(before, after)
            version = {
                "version": len(code["versions"]) + 1,
                "created": time.time(),
                "source": source,
                "note": note,
                "files": after,
                "changed": sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)),
                "diff": diff,
            }
            code["versions"].append(version)
            code["files"] = after
            self.project["changes"].append({"ts": version["created"], "type": "code",
                                            "description": f"v{version['version']} ({source}): {note}".strip(),
                                            "files": version["changed"]})
            self.touch()
            return version

    # ------------------------------------------------------------ persistence
    def save(self) -> str:
        with self.lock:
            data = json.dumps(self.project, ensure_ascii=False)
            path = self.projects_dir / f"{self.project['id']}.json"
            self._dirty = False
        _atomic_write(path, data)
        return str(path)

    def autosave(self) -> None:
        if self._dirty:
            try:
                self.save()
            except OSError as e:
                self.log("error", f"Automatikus mentés sikertelen: {e}")

    def list_saved(self) -> list[dict]:
        out = []
        for p in sorted(self.projects_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(p.read_text("utf-8"))
                out.append({"id": data.get("id", p.stem), "name": data.get("name", p.stem),
                            "updated": data.get("updated"), "task": (data.get("task") or "")[:160],
                            "size": p.stat().st_size})
            except (OSError, ValueError):
                continue
        return out

    def load(self, project_id: str) -> dict:
        if not re.match(r"^[\w-]+$", project_id or ""):
            raise ValueError("Érvénytelen projekt azonosító.")
        path = self.projects_dir / f"{project_id}.json"
        data = migrate(json.loads(path.read_text("utf-8")))
        with self.lock:
            self.project = data
            self._dirty = False
        return data

    def delete_saved(self, project_id: str) -> bool:
        if not re.match(r"^[\w-]+$", project_id or ""):
            return False
        path = self.projects_dir / f"{project_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def new(self, name: str = "", keep_settings: bool = True) -> dict:
        with self.lock:
            llms = self.project["llms"] if keep_settings else None
            settings = self.project["settings"] if keep_settings else None
            self.project = new_project(name, llms=llms, settings=settings)
            self._dirty = True
        return self.project

    def export(self, include_keys: bool = False) -> dict:
        data = self.snapshot()
        if not include_keys:
            for cfg in data["llms"].values():
                cfg["api_key"] = ""
        data["exported"] = time.time()
        return data

    def import_project(self, data: dict) -> dict:
        data = migrate(copy.deepcopy(data))
        data.pop("exported", None)
        with self.lock:
            # Keep locally configured API keys when the export was stripped of them.
            for slot, cfg in data["llms"].items():
                if not cfg.get("api_key"):
                    cfg["api_key"] = self.project["llms"].get(slot, {}).get("api_key", "")
            self.project = data
            self.touch()
        return data


class _Mutation:
    """``with store.mutate() as p:`` – locked, marks the project dirty."""

    def __init__(self, store: ProjectStore):
        self.store = store

    def __enter__(self) -> dict:
        self.store.lock.acquire()
        return self.store.project

    def __exit__(self, *exc) -> None:
        try:
            self.store.touch()
        finally:
            self.store.lock.release()


def unified_diff(before: dict[str, str], after: dict[str, str], limit: int = 60000) -> str:
    chunks = []
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name), after.get(name)
        if a == b:
            continue
        chunks.extend(difflib.unified_diff(
            (a or "").splitlines(keepends=True), (b or "").splitlines(keepends=True),
            fromfile=f"a/{name}" if a is not None else "/dev/null",
            tofile=f"b/{name}" if b is not None else "/dev/null"))
    text = "".join(chunks)
    return text if len(text) <= limit else text[:limit] + "\n... (diff rövidítve)\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)
