"""Self-test / diagnostics: checks every feature on the user's machine.

Run from the UI (Diagnosztika) or the console:  python -m llm_arena --selftest
The functional checks use built-in mock models on free local ports, so they
test the program itself independently of the user's llama-servers; the
configured endpoints, the web and the browser are checked separately.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import platform
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable

from . import __version__

ROOT = Path(__file__).resolve().parent


def version_info() -> dict:
    info = {"version": __version__, "python": sys.version.split()[0], "os": f"{platform.system()} {platform.release()}",
            "path": str(ROOT), "commit": "", "branch": ""}
    git = ROOT.parent / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1]
            info["branch"] = ref.rsplit("/", 1)[-1]
            ref_file = git / ref
            if ref_file.exists():
                info["commit"] = ref_file.read_text().strip()[:10]
            else:
                packed = (git / "packed-refs").read_text()
                for line in packed.splitlines():
                    if line.endswith(ref):
                        info["commit"] = line.split()[0][:10]
        else:
            info["commit"] = head[:10]
    except OSError:
        pass
    return info


class CheckFailed(Exception):
    """A live check did not pass (not a program error)."""

    def to_dict(self) -> dict:
        return {"kind": "check", "label": "Nem sikerült", "message": str(self), "hint": ""}


def _check(name: str, fn: Callable[[], str | None], results: list[dict], hint: str = "") -> bool:
    t0 = time.monotonic()
    try:
        detail = fn() or ""
        results.append({"name": name, "ok": True, "detail": detail, "duration": round(time.monotonic() - t0, 2)})
        return True
    except Exception as e:  # noqa: BLE001
        from .errors import describe_exception
        err = describe_exception(e)
        results.append({"name": name, "ok": False, "detail": f"{err.get('label')}: {err.get('message')}",
                        "hint": hint or err.get("hint"),
                        "trace": "" if isinstance(e, CheckFailed) else traceback.format_exc()[-2500:],
                        "duration": round(time.monotonic() - t0, 2)})
        return False


def _wait(job, timeout: float = 60) -> None:
    t0 = time.monotonic()
    while not job.done:
        if time.monotonic() - t0 > timeout:
            raise TimeoutError(f"a folyamat nem fejeződött be {timeout:.0f} s alatt")
        time.sleep(0.05)
    if job.status != "done":
        err = job.error or {}
        raise RuntimeError(f"{err.get('label', job.status)}: {err.get('message', '')}")


def run(settings: dict | None = None, llms: dict | None = None, data_dir: str | None = None,
        quick: bool = False) -> dict:
    """Run all checks. ``settings``/``llms`` are the user's current configuration (for the live checks)."""
    results: list[dict] = []
    settings = settings or {}

    # 1. modules and static files ------------------------------------------------
    def imports() -> str:
        names = [m.name for m in pkgutil.walk_packages([str(ROOT)], "llm_arena.") if "__main__" not in m.name]
        for n in names:
            importlib.import_module(n)
        return f"{len(names)} modul"
    _check("Programmodulok betöltése", imports, results, "A telepítés hiányos – töltsd le újra a teljes kódot (git pull).")

    def static() -> str:
        from .server import STATIC_DIR, STATIC_TYPES
        need = ["index.html", "css/app.css", "js/app.js", "js/api.js", "js/ui.js", "js/state.js", "js/md.js"] + [
            f"js/views/{v}.js" for v in ("settings", "arena", "debate", "design", "code", "testing", "consensus",
                                         "pipeline", "log", "project", "diagnostics")]
        missing = [f for f in need if not (STATIC_DIR / f).is_file()]
        if missing:
            raise FileNotFoundError("hiányzó felületfájlok: " + ", ".join(missing))
        if not STATIC_TYPES[".js"].startswith("text/javascript"):
            raise RuntimeError("hibás JS MIME-típus")
        return f"{len(need)} fájl rendben"
    _check("Felület fájljai", static, results, "Töltsd le újra a teljes kódot; ne csak egyes fájlokat másolj át.")

    def data_writable() -> str:
        d = Path(data_dir or "data")
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".selftest_{os.getpid()}.tmp"
        probe.write_text("ok", "utf-8")
        probe.unlink()
        return str(d.resolve())
    _check("Adatmappa írható", data_writable, results)

    # 2. functional checks with built-in mock models ------------------------------
    from .app import ArenaApp
    from .mock_server import MockLlama
    a = b = None
    try:
        a = MockLlama(name="selftest-a").start()
        b = MockLlama(name="selftest-b").start()
        app = ArenaApp(tempfile.mkdtemp(prefix="arena_selftest_"), autosave_interval=0)
        app.update_config({"llms": {"A": {"base_url": a.url, "retries": 0}, "B": {"base_url": b.url, "retries": 0}},
                           "settings": {"web_enabled": False, "allow_code_execution": True}})

        def arena() -> str:
            job = app.start_arena("Önteszt kérdés")
            _wait(job)
            if job.result.get("failed"):
                raise RuntimeError(f"sikertelen modellek: {job.result['failed']}")
            return "mindkét modell válaszolt"
        _check("Aréna (párhuzamos válasz)", arena, results)
        _check("Közös döntés", lambda: (_wait(app.start_consensus()), "kész")[1], results)
        _check("Vita (1 kör + összegzés)", lambda: (_wait(app.start_debate("Önteszt téma", rounds=1)), "kész")[1],
               results)
        if not quick:
            def design() -> str:
                _wait(app.start_design("Számológép modul (add, divide)"), 120)
                rep = app.store.snapshot()["testing"].get("report") or {}
                return f"11 lépés kész, tesztek: {rep.get('passed', 0)}/{rep.get('total', 0)}"
            _check("Programtervezés + tesztelés + javító ciklus", design, results)

        def exports() -> str:
            from . import report
            report.render(app.store.snapshot(), "all")
            app.conversation_markdown()
            app.store.export()
            return "HTML, Markdown, projekt JSON"
        _check("Exportok", exports, results)

        def attach() -> str:
            import base64
            meta = app.upload_attachment("teszt.txt", base64.b64encode("árvíztűrő".encode()).decode())
            _wait(app.start_arena("Mi van a fájlban?", attachments=[meta["id"]]))
            return "szöveges fájl csatolva és elküldve"
        _check("Fájlcsatolás", attach, results)

        def save_load() -> str:
            pid = app.store.project["id"]
            app.store.save()
            app.store.load(pid)
            return "mentés és visszatöltés"
        _check("Projekt mentése / betöltése", save_load, results)
    except Exception as e:  # noqa: BLE001
        results.append({"name": "Belső teszt-környezet", "ok": False, "detail": f"{type(e).__name__}: {e}",
                         "trace": traceback.format_exc()[-2500:]})
    finally:
        for m in (a, b):
            if m:
                try:
                    m.stop()
                except Exception:  # noqa: BLE001
                    pass

    def sandbox_check() -> str:
        from . import sandbox
        rep = sandbox.run_tests({"m.py": "X = 1\n", "test_m.py": "import unittest, m\n"
                                 "class T(unittest.TestCase):\n    def test_x(self): self.assertEqual(m.X, 1)\n"},
                                timeout=30)
        if not rep["summary"].get("all_passed"):
            raise RuntimeError(rep.get("error") or rep.get("stderr") or "a próbateszt nem futott le")
        return "kódfuttatás működik"
    _check("Tesztfuttató (sandbox)", sandbox_check, results)

    # 3. the user's own configuration -----------------------------------------------
    from .providers import LLMConfig, create_provider
    for slot in ("A", "B"):
        cfg = LLMConfig.from_dict((llms or {}).get(slot), slot)

        def conn(cfg=cfg) -> str:
            res = create_provider(cfg).test_connection(probe_completion=True)
            if not res["ok"]:
                bad = next((s for s in res["steps"] if not s.get("ok")), {})
                e = bad.get("error") or {}
                raise CheckFailed(f"{cfg.base_url} – {bad.get('step', '?')}: {e.get('label', '')}: {e.get('message', '')}")
            return f"{cfg.base_url} · modell: {res.get('model') or '?'}"
        _check(f"Saját LLM {slot} kapcsolat", conn, results,
               "Fut a llama-server ezen a címen? Ellenőrizd az URL-t és a portot a Beállításokban.")

    if settings.get("web_enabled"):
        def web_search() -> str:
            from . import tools
            r = tools.execute("web_search", {"query": "Python programming language"}, tools.web_config(settings))
            if not r["ok"]:
                raise CheckFailed(r["summary"])
            return r["summary"]
        _check("Webes keresés", web_search, results,
               "Ellenőrizd az internetet/proxyt; tartós hibánál indíts helyi SearXNG-t (Beállítások).")
    from .tools import browser
    inst = browser.installed()
    results.append({"name": "Böngésző (Playwright + stealth)", "ok": True,
                    "detail": ("telepítve" if inst["playwright"] else "nincs telepítve (opcionális)")
                    + (", stealth: igen" if inst["stealth"] else ", stealth: nem")})
    ok = sum(1 for r in results if r["ok"])
    return {"version": version_info(), "results": results, "passed": ok, "failed": len(results) - ok,
            "created": time.time()}


def print_report(rep: dict) -> None:
    v = rep["version"]
    print(f"LLM Aréna {v['version']} · commit {v['commit'] or '?'} ({v['branch'] or '?'}) · Python {v['python']} · {v['os']}")
    print(f"Hely: {v['path']}\n")
    for r in rep["results"]:
        print(f"{'OK  ' if r['ok'] else 'HIBA'}  {r['name']}: {r.get('detail', '')}")
        if not r["ok"] and r.get("hint"):
            print(f"      tipp: {r['hint']}")
    print(f"\nÖsszesen: {rep['passed']} rendben, {rep['failed']} hiba")
