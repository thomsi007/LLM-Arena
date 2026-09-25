"""Execute generated code and its unittest-based tests in a throw-away directory.

This is *process isolation*, not a security sandbox: the code runs as the
current user with a scrubbed environment, a temp working directory, a wall
clock timeout and (on POSIX) CPU / memory / file size limits. Execution must
be explicitly enabled in the settings.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .textutil import clip, is_test_file, safe_path

RESULT_FILE = "__arena_results.jsonl"

# Runner executed inside the sandbox. Writes one JSON line per event so partial
# results survive a timeout. Works with unittest-style tests; plain pytest style
# `def test_x()` functions are wrapped too, so both conventions are accepted.
RUNNER = r'''
import importlib.util, inspect, io, json, os, sys, time, traceback, unittest
OUT = open(sys.argv[1], "a", encoding="utf-8")
def w(obj):
    OUT.write(json.dumps(obj, ensure_ascii=False) + "\n"); OUT.flush()
sys.path.insert(0, os.getcwd())

class R(unittest.TestResult):
    def startTest(self, test):
        super().startTest(test); self._t = time.perf_counter(); w({"ev": "start", "id": test.id()})
    def _rec(self, test, status, err=None):
        tb = ""
        if err is not None:
            tb = "".join(traceback.format_exception(*err)) if isinstance(err, tuple) else str(err)
        w({"ev": "result", "id": test.id(), "status": status, "tb": tb[-4000:],
           "duration": round(time.perf_counter() - getattr(self, "_t", time.perf_counter()), 4)})
    def addSuccess(self, test): super().addSuccess(test); self._rec(test, "passed")
    def addFailure(self, test, err): super().addFailure(test, err); self._rec(test, "failed", err)
    def addError(self, test, err): super().addError(test, err); self._rec(test, "error", err)
    def addSkip(self, test, reason): super().addSkip(test, reason); self._rec(test, "skipped", reason)
    def addExpectedFailure(self, test, err): super().addExpectedFailure(test, err); self._rec(test, "passed")
    def addUnexpectedSuccess(self, test): super().addUnexpectedSuccess(test); self._rec(test, "failed", "unexpected success")

def load_module_suite(path):
    name = os.path.splitext(os.path.relpath(path))[0].replace(os.sep, ".")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
    # pytest-style module level test functions
    for attr, obj in list(vars(mod).items()):
        if attr.startswith("test") and inspect.isfunction(obj) and obj.__module__ == name:
            if not inspect.signature(obj).parameters:
                suite.addTest(unittest.FunctionTestCase(obj, description=f"{name}.{attr}"))
    return suite

def main():
    files = json.loads(sys.argv[2])
    suite = unittest.TestSuite()
    for f in files:
        try:
            suite.addTest(load_module_suite(f))
        except BaseException:
            w({"ev": "result", "id": f"{f}::<import>", "status": "error", "duration": 0,
               "tb": traceback.format_exc()[-4000:]})
    buf_out, buf_err = io.StringIO(), io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        suite.run(R())
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        w({"ev": "end", "stdout": buf_out.getvalue()[-6000:], "stderr": buf_err.getvalue()[-6000:]})

main()
'''

CATEGORY_HINTS = (
    ("performance", ("perf", "performance", "benchmark", "speed")),
    ("error_handling", ("error", "exception", "hibakezel", "invalid", "raises")),
    ("edge_case", ("edge", "boundary", "corner", "hatar")),
    ("integration", ("integration", "integr", "e2e", "end_to_end")),
    ("unit", ("unit",)),
)


def categorize(test_id: str, file: str) -> str:
    """Category from the test class / method name first, then from the file name."""
    local = test_id.split("::")[-1]
    mod = file[:-3].replace("/", ".") if file.endswith(".py") else ""
    if mod and local.startswith(mod + "."):
        local = local[len(mod) + 1:]
    for text in (local.lower(), file.rsplit("/", 1)[-1].lower()):
        for cat, hints in CATEGORY_HINTS:
            if any(h in text for h in hints):
                return cat
    return "unit"


def _limits(timeout: int):  # pragma: no cover - runs in the child
    def apply() -> None:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5, timeout + 10))
            mem = 2 * 1024 ** 3
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 ** 2, 64 * 1024 ** 2))
        except Exception:
            pass
    return apply


def run_tests(files: dict[str, str], *, timeout: int = 60, python: str | None = None) -> dict:
    """Write files to a temp dir, run all test files and return a structured report."""
    start = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="llm_arena_"))
    written: list[str] = []
    rejected: list[str] = []
    try:
        for name, content in files.items():
            p = safe_path(name)
            if not p:
                rejected.append(name)
                continue
            target = workdir / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, "utf-8")
            written.append(p)
        # Make sub directories importable as packages.
        for d in {Path(p).parent for p in written if "/" in p}:
            init = workdir / d / "__init__.py"
            if not init.exists():
                init.write_text("", "utf-8")
        test_files = sorted(p for p in written if is_test_file(p) and p.endswith(".py"))
        (workdir / "__arena_runner.py").write_text(RUNNER, "utf-8")
        result_path = workdir / RESULT_FILE
        report = {
            "started": start, "duration": 0.0, "timed_out": False, "returncode": None,
            "test_files": test_files, "rejected_files": rejected,
            "tests": [], "summary": {}, "stdout": "", "stderr": "", "error": None,
        }
        if not test_files:
            report["error"] = "Nincs futtatható tesztfájl (test_*.py)."
            report["summary"] = _summary([])
            return report
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "PYTHONPATH": str(workdir),
        }
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "C:\\Windows")
        cmd = [python or sys.executable, "-B", "__arena_runner.py", str(result_path), json.dumps(test_files)]
        kwargs: dict = {"cwd": workdir, "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                        "stdin": subprocess.DEVNULL}
        if os.name == "posix":
            kwargs["start_new_session"] = True
            kwargs["preexec_fn"] = _limits(timeout)
        proc = subprocess.Popen(cmd, **kwargs)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            report["timed_out"] = True
            _kill(proc)
            out, err = proc.communicate()
        report["returncode"] = proc.returncode
        report["stdout"] = clip(out.decode("utf-8", "replace"), 6000)
        report["stderr"] = clip(err.decode("utf-8", "replace"), 6000)
        report["tests"] = _parse_results(result_path, report)
        report["summary"] = _summary(report["tests"])
        if not report["tests"] and not report["error"]:
            report["error"] = "A tesztfuttató nem adott eredményt. " + report["stderr"][-1500:]
        return report
    finally:
        try:
            report["duration"] = round(time.time() - start, 3)  # type: ignore[possibly-undefined]
        except NameError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _parse_results(path: Path, report: dict) -> list[dict]:
    tests: dict[str, dict] = {}
    started: list[str] = []
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("ev") == "start":
            started.append(ev["id"])
        elif ev.get("ev") == "result":
            tests[ev["id"]] = ev
        elif ev.get("ev") == "end":
            report["stdout"] = clip((ev.get("stdout") or "") + report["stdout"], 6000)
            report["stderr"] = clip((ev.get("stderr") or "") + report["stderr"], 6000)
    for sid in started:
        if sid not in tests:
            tests[sid] = {"id": sid, "status": "timeout" if report["timed_out"] else "error",
                          "tb": "A teszt nem fejeződött be (időtúllépés vagy összeomlás).", "duration": None}
    out = []
    files = report["test_files"]
    for tid, ev in tests.items():
        file = _file_for(tid, files)
        tb = ev.get("tb") or ""
        out.append({
            "id": tid,
            "name": tid.split(".")[-1].split("::")[-1],
            "file": file,
            "category": categorize(tid, file),
            "status": ev.get("status", "error"),
            "message": _last_line(tb),
            "traceback": tb,
            "duration": ev.get("duration"),
        })
    out.sort(key=lambda t: (t["file"], t["id"]))
    return out


def _file_for(test_id: str, files: list[str]) -> str:
    if "::" in test_id:
        return test_id.split("::")[0]
    for f in files:
        mod = f[:-3].replace("/", ".")
        if test_id.startswith(mod + ".") or test_id == mod:
            return f
    return files[0] if len(files) == 1 else ""


def _last_line(tb: str) -> str:
    lines = [ln for ln in (tb or "").strip().splitlines() if ln.strip()]
    return lines[-1][:400] if lines else ""


def _summary(tests: list[dict]) -> dict:
    s = {"total": len(tests), "passed": 0, "failed": 0, "error": 0, "skipped": 0, "timeout": 0, "by_category": {}}
    for t in tests:
        s[t["status"]] = s.get(t["status"], 0) + 1
        cat = s["by_category"].setdefault(t["category"], {"total": 0, "passed": 0})
        cat["total"] += 1
        if t["status"] == "passed":
            cat["passed"] += 1
    s["failing"] = s["failed"] + s["error"] + s["timeout"]
    s["all_passed"] = s["total"] > 0 and s["failing"] == 0
    return s
