"""Automatic test generation, execution and fix loop.

Developer model writes unit + integration tests, reviewer writes edge-case,
error-handling and (where meaningful) performance tests. Failing tests are
sent back to both models for analysis, the developer fixes code (or a wrong
test), tests are re-run – up to ``max_fix_iterations`` times.
"""

from __future__ import annotations

import time
import uuid

from .. import prompts, sandbox
from ..textutil import clip, extract_files, is_test_file, safe_path
from .common import StepFailed, WorkflowContext, other

FAILING = ("failed", "error", "timeout")


class ExecutionDisabled(StepFailed):
    def __init__(self) -> None:
        super().__init__("tests", {"kind": "execution_disabled", "label": "Kódfuttatás tiltva",
                                   "message": "A generált kód futtatása ki van kapcsolva (Beállítások → "
                                              "Kódfuttatás engedélyezése)."})


def roles(ctx: WorkflowContext) -> tuple[str, str]:
    dev = ctx.settings.get("developer", "A")
    return dev, other(dev)


def code_listing(files: dict[str, str], budget: int, *, tests: bool | None = None) -> str:
    """Files as FILE: blocks; ``tests`` None = all, True = only tests, False = only code."""
    names = [n for n in sorted(files) if tests is None or is_test_file(n) == tests]
    if not names:
        return "(nincs fájl)"
    per = max(1500, budget // max(1, len(names)))
    parts = []
    for n in names:
        lang = "python" if n.endswith(".py") else ""
        parts.append(f"FILE: {n}\n```{lang}\n{clip(files[n], per)}\n```")
    return "\n\n".join(parts)


def failures_text(run: dict, limit: int = 12, tb_chars: int = 1500) -> str:
    failing = [t for t in run.get("tests", []) if t["status"] in FAILING]
    parts = []
    for t in failing[:limit]:
        parts.append(f"- {t['id']} [{t['status']}] ({t['file']})\n```\n{clip(t['traceback'] or t['message'], tb_chars)}\n```")
    if len(failing) > limit:
        parts.append(f"... és további {len(failing) - limit} sikertelen teszt")
    if run.get("error"):
        parts.append(f"Futtatási hiba: {run['error']}")
    if run.get("timed_out"):
        parts.append("A tesztfuttatás időtúllépés miatt leállt.")
    if not failing and run.get("stderr"):
        parts.append("stderr:\n```\n" + clip(run["stderr"], 2000) + "\n```")
    return "\n".join(parts) or "(nincs részlet)"


def _state(ctx: WorkflowContext) -> dict:
    return ctx.store.project["testing"]


def _files(ctx: WorkflowContext) -> dict[str, str]:
    with ctx.store.lock:
        return dict(ctx.store.project["code"]["files"])


def _accept_files(files: dict[str, str]) -> dict[str, str]:
    return {p: c for n, c in files.items() if (p := safe_path(n))}


# --------------------------------------------------------------------------- #
def generate_tests(ctx: WorkflowContext, requirements: str) -> dict[str, str]:
    code = {k: v for k, v in _files(ctx).items() if not is_test_file(k)}
    if not code:
        raise StepFailed("tests", {"kind": "no_code", "label": "Nincs kód", "message": "Nincs tesztelhető kód."})
    dev, rev = roles(ctx)
    ctx.progress(0.05, f"Tesztek generálása (LLM {dev}: unit+integrációs, LLM {rev}: edge/hiba/teljesítmény)")

    def ask(slot: str, template: str, title: str, system: str):
        text = ctx.fmt(template, requirements=ctx.clip_for(slot, requirements, 0.2),
                       code=code_listing(code, ctx.budget(slot, 0.6), tests=False))
        return ctx.call(slot, text, role="tester", stage="tests", title=title, system=system)

    results = ctx.parallel({
        dev: lambda: ask(dev, prompts.TESTS_DEV, "Unit + integrációs tesztek", ctx.fmt(prompts.DEV_SYSTEM)),
        rev: lambda: ask(rev, prompts.TESTS_REV, "Edge-case, hibakezelési, teljesítménytesztek",
                         ctx.fmt(prompts.REVIEWER_SYSTEM)),
    })
    generated: dict[str, str] = {}
    by_slot: dict[str, list[str]] = {}
    msg_ids = []
    for slot, (msg, err) in results.items():
        if err is not None:
            ctx.log("warning", f"LLM {slot} tesztgenerálása sikertelen: {err}")
            continue
        msg_ids.append(msg["id"])
        default = "test_unit.py" if slot == dev else "test_edge_cases.py"
        files = {k: v for k, v in _accept_files(extract_files(msg["content"], default_name=default)).items()}
        # Everything a tester returns is a test file; rename accidental non-test names.
        clean = {}
        for name, content in files.items():
            if not is_test_file(name):
                name = f"test_{slot.lower()}_{name.rsplit('/', 1)[-1]}"
            clean[name] = content
        by_slot[slot] = sorted(clean)
        generated.update(clean)
    if not generated:
        raise StepFailed("tests", {"kind": "no_tests", "label": "Nincs teszt",
                                   "message": "Egyik modell sem adott értelmezhető tesztfájlt."})
    ctx.store.set_code(generated, source="tests", note="Generált tesztek: " + ", ".join(sorted(generated)))
    with ctx.store.mutate():
        _state(ctx)["generated"] = {"by_slot": by_slot, "messages": msg_ids, "created": time.time()}
    ctx.state_changed("code")
    ctx.state_changed("testing")
    ctx.log("info", f"{len(generated)} tesztfájl generálva: {', '.join(sorted(generated))}")
    return generated


def run_suite(ctx: WorkflowContext, iteration: int = 0, label: str = "") -> dict:
    s = ctx.settings
    if not s.get("allow_code_execution"):
        raise ExecutionDisabled()
    ctx.check()
    files = _files(ctx)
    ctx.progress(ctx.job.progress, label or f"Tesztek futtatása ({iteration}. iteráció)")
    report = sandbox.run_tests(files, timeout=int(s.get("test_timeout", 60)))
    with ctx.store.lock:
        versions = ctx.store.project["code"]["versions"]
        version = versions[-1]["version"] if versions else 0
    run = {"id": uuid.uuid4().hex[:8], "iteration": iteration, "created": time.time(),
           "code_version": version, **report}
    with ctx.store.mutate():
        _state(ctx)["runs"].append(run)
    sm = run["summary"]
    ctx.log("info" if sm.get("all_passed") else "warning",
            f"Teszteredmény (v{version}): {sm.get('passed', 0)}/{sm.get('total', 0)} sikeres, "
            f"{sm.get('failing', 0)} sikertelen{' – IDŐTÚLLÉPÉS' if run['timed_out'] else ''}")
    ctx.state_changed("testing")
    return run


def _analyse(ctx: WorkflowContext, requirements: str, run: dict, iteration: int) -> tuple[list[dict], list[str]]:
    dev, rev = roles(ctx)
    files = _files(ctx)
    fails = failures_text(run)

    def ask(slot: str):
        text = ctx.fmt(prompts.TEST_ANALYSIS, requirements=ctx.clip_for(slot, requirements, 0.15),
                       code=code_listing(files, ctx.budget(slot, 0.5)), failures=clip(fails, ctx.budget(slot, 0.25)))
        return ctx.call_json(slot, text, role="analyst", stage="test_analysis", round=iteration,
                             title=f"Hibaelemzés – {iteration}. iteráció", temperature=0.2,
                             system=ctx.fmt(prompts.REVIEWER_SYSTEM if slot == rev else prompts.DEV_SYSTEM))

    results = ctx.parallel({rev: lambda: ask(rev), dev: lambda: ask(dev)})
    findings: list[dict] = []
    msg_ids: list[str] = []
    for slot in (rev, dev):
        res, err = results.get(slot, (None, None))
        if err is not None or not res:
            continue
        msg, data = res
        msg_ids.append(msg["id"])
        for f in (data or {}).get("failures") or []:
            if isinstance(f, dict):
                findings.append({"by": slot, "test": str(f.get("test") or ""), "root_cause": str(f.get("root_cause") or ""),
                                 "fix_target": str(f.get("fix_target") or "code"),
                                 "location": str(f.get("location") or ""), "suggestion": str(f.get("suggestion") or "")})
    if not msg_ids:
        raise StepFailed("test_analysis", {"kind": "analysis_failed", "label": "Elemzés sikertelen",
                                           "message": "Egyik modell sem tudta elemezni a hibákat."})
    return findings, msg_ids


def _analysis_text(findings: list[dict]) -> str:
    if not findings:
        return "(a modellek nem adtak strukturált elemzést – a hibaüzenetek alapján javíts)"
    return "\n".join(f"- [{f['by']}] {f['test']}: {f['root_cause']} → javítandó: {f['fix_target']} "
                     f"({f['location']}); javaslat: {f['suggestion']}" for f in findings)


def fix_loop(ctx: WorkflowContext, requirements: str, max_iterations: int | None = None) -> dict:
    s = ctx.settings
    max_iterations = int(max_iterations if max_iterations is not None else s.get("max_fix_iterations", 3))
    dev, _ = roles(ctx)
    loop_id = uuid.uuid4().hex[:8]
    run = run_suite(ctx, 0, "Tesztek futtatása (kiinduló állapot)")
    iteration = 0
    while not run["summary"].get("all_passed") and iteration < max_iterations:
        iteration += 1
        ctx.check()
        base = 0.2 + 0.7 * (iteration - 1) / max(1, max_iterations)
        failing_ids = [t["id"] for t in run["tests"] if t["status"] in FAILING]
        rec = {"loop": loop_id, "iteration": iteration, "created": time.time(), "failing_before": failing_ids,
               "findings": [], "messages": [], "changed_files": [], "fixed": [], "failing_after": [],
               "status": "running"}
        with ctx.store.mutate():
            _state(ctx)["iterations"].append(rec)
        ctx.state_changed("testing")

        ctx.progress(base, f"{iteration}. javító iteráció – hibák elemzése")
        findings, msg_ids = _analyse(ctx, requirements, run, iteration)
        with ctx.store.mutate():
            rec["findings"] = findings
            rec["messages"] += msg_ids
        ctx.state_changed("testing")

        ctx.progress(base + 0.1, f"{iteration}. javító iteráció – javítás (LLM {dev})")
        files = _files(ctx)
        text = ctx.fmt(prompts.TEST_FIX, requirements=ctx.clip_for(dev, requirements, 0.12),
                       code=code_listing(files, ctx.budget(dev, 0.5)),
                       failures=clip(failures_text(run), ctx.budget(dev, 0.18)),
                       analysis=clip(_analysis_text(findings), ctx.budget(dev, 0.15)),
                       code_rules=ctx.fmt(prompts.CODE_RULES))
        msg = ctx.call(dev, text, role="developer", stage="test_fix", round=iteration,
                       title=f"Javítás – {iteration}. iteráció", system=ctx.fmt(prompts.DEV_SYSTEM))
        with ctx.store.mutate():
            rec["messages"].append(msg["id"])
        changed = {n: c for n, c in _accept_files(extract_files(msg["content"], default_name="")).items()
                   if n and files.get(n) != c}
        changed.pop("", None)
        if not changed:
            with ctx.store.mutate():
                rec["status"] = "no_change"
            ctx.log("warning", f"{iteration}. iteráció: a javítás nem módosított egyetlen fájlt sem – a ciklus leáll.")
            ctx.state_changed("testing")
            break
        version = ctx.store.set_code(changed, source=f"test_fix_{iteration}",
                                     note=f"Tesztjavítás {iteration}. iteráció")
        with ctx.store.mutate():
            rec["changed_files"] = sorted(changed)
            rec["code_version"] = version["version"]
        ctx.state_changed("code")

        new_run = run_suite(ctx, iteration, f"{iteration}. iteráció – tesztek újrafuttatása")
        status = {t["id"]: t["status"] for t in new_run["tests"]}
        fixed = [tid for tid in failing_ids if status.get(tid) == "passed"]
        with ctx.store.mutate():
            rec["fixed"] = fixed
            rec["failing_after"] = [t["id"] for t in new_run["tests"] if t["status"] in FAILING]
            rec["status"] = "done"
            for tid in fixed:
                f = next((f for f in findings if f["test"] and (f["test"] in tid or tid.endswith(f["test"]))), None)
                _state(ctx)["fixed_bugs"].append({
                    "test": tid, "iteration": iteration, "code_version": version["version"],
                    "root_cause": (f or {}).get("root_cause", ""), "fix_target": (f or {}).get("fix_target", ""),
                    "by": dev, "loop": loop_id,
                })
        ctx.state_changed("testing")
        run = new_run

    report = build_report(ctx, run, iteration, max_iterations, loop_id)
    ctx.progress(1.0, "Tesztelés kész" + (" – minden teszt sikeres" if report["all_passed"] else ""))
    return report


def build_report(ctx: WorkflowContext, run: dict, iterations: int, max_iterations: int,
                 loop_id: str | None = None) -> dict:
    sm = run["summary"]
    with ctx.store.lock:
        t = _state(ctx)
        fixed = [b for b in t["fixed_bugs"] if loop_id is None or b.get("loop") == loop_id]
        code_files = dict(ctx.store.project["code"]["files"])
        versions = ctx.store.project["code"]["versions"]
    remaining = [f"{x['id']}: {x['message'] or x['status']}" for x in run["tests"] if x["status"] in FAILING]
    if run.get("error"):
        remaining.append(run["error"])
    report = {
        "created": time.time(),
        "passed": sm.get("passed", 0),
        "failed": sm.get("failing", 0),
        "total": sm.get("total", 0),
        "skipped": sm.get("skipped", 0),
        "all_passed": bool(sm.get("all_passed")),
        "by_category": sm.get("by_category", {}),
        "iterations": iterations,
        "max_iterations": max_iterations,
        "fixed_bugs": fixed,
        "remaining_issues": remaining,
        "final_version": versions[-1]["version"] if versions else 0,
        "final_files": sorted(code_files),
        "last_run": run["id"],
    }
    with ctx.store.mutate():
        _state(ctx)["report"] = report
    ctx.state_changed("testing")
    return report


def summary_text(report: dict | None) -> str:
    if not report:
        return "(a tesztek nem futottak)"
    lines = [f"Sikeres: {report['passed']} / {report['total']}, sikertelen: {report['failed']}, "
             f"javító iterációk: {report['iterations']}, javított hibák: {len(report['fixed_bugs'])}"]
    if report["remaining_issues"]:
        lines.append("Fennmaradó problémák:\n" + "\n".join(f"- {r}" for r in report["remaining_issues"][:10]))
    return "\n".join(lines)
