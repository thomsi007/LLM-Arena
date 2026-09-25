"""Collaborative Coding / Program Designer.

Stages 1-11 (requirements → … → final version). One model is the primary
developer, the other the reviewer / auditor. Every stage keeps its text and
parsed JSON in ``project['design']['stages']`` so the run can be resumed.
"""

from __future__ import annotations

import time
import uuid

from .. import prompts
from ..textutil import as_list, clip, extract_files, extract_json, is_test_file, remove_json_blocks, safe_path
from . import testing
from .common import StepFailed, WorkflowContext, other
from .consensus import run_consensus

STAGES = prompts.DESIGN_STAGES
STAGE_KEYS = [s[0] for s in STAGES]
FIX_SEVERITIES = ("critical", "high", "medium")


def new_design(requirements: str, developer: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:10], "requirements": requirements.strip(), "developer": developer,
        "reviewer": other(developer), "status": "running", "created": time.time(), "error": None,
        "order": STAGE_KEYS,
        "stages": {k: {"key": k, "label": label, "actor": actor, "status": "pending", "messages": [],
                       "text": "", "data": None, "note": ""} for k, label, actor, _ in STAGES},
        "review_issues": [], "final": None,
    }


def _prev(design: dict, budget: int) -> dict[str, str]:
    share = max(1500, budget // 5)
    out = {}
    for k, st in design["stages"].items():
        out[f"prev_{k}"] = clip(st.get("text") or "(nincs)", share)
    return out


def _slot(design: dict, actor: str) -> str:
    return design["developer"] if actor == "dev" else design["reviewer"]


def _set_stage(ctx: WorkflowContext, st: dict, **values) -> None:
    with ctx.store.mutate():
        st.update(values)
    ctx.state_changed("design")


def _issues_text(issues: list[dict]) -> str:
    if not issues:
        return "(nincs)"
    return "\n".join(f"- {i.get('id', '')} [{i.get('severity', '?')}/{i.get('category', '?')}] "
                     f"{i.get('location', '')}: {i.get('description', '')} → {i.get('suggestion', '')}" for i in issues)


def _normalize_issues(data) -> list[dict]:
    out = []
    for n, i in enumerate(as_list((data or {}).get("issues") if isinstance(data, dict) else None), 1):
        if isinstance(i, dict):
            out.append({"id": str(i.get("id") or f"R{n}"),
                        "category": str(i.get("category") or "logic").lower(),
                        "severity": str(i.get("severity") or "medium").lower(),
                        "location": str(i.get("location") or ""),
                        "description": str(i.get("description") or ""),
                        "suggestion": str(i.get("suggestion") or ""), "status": "open"})
        elif str(i).strip():
            out.append({"id": f"R{n}", "category": "logic", "severity": "medium", "location": "",
                        "description": str(i), "suggestion": "", "status": "open"})
    return out


def run_design(ctx: WorkflowContext, *, requirements: str | None = None, developer: str | None = None,
               resume: bool = False, run_tests: bool = True, stages: list[str] | None = None) -> dict:
    settings = ctx.settings
    with ctx.store.mutate() as p:
        if not resume or not p.get("design") or not p["design"].get("requirements"):
            if not (requirements or "").strip():
                raise ValueError("Adj meg követelményeket / feladatleírást.")
            p["design"] = new_design(requirements, developer or settings.get("developer", "A"))
            if not p["task"]:
                p["task"] = requirements.strip()
        design = p["design"]
        design["status"] = "running"
        design["error"] = None
        for st in design["stages"].values():
            if st["status"] in ("running", "error", "cancelled"):
                st["status"] = "pending"
    ctx.state_changed("design")
    keys = [k for k in STAGE_KEYS if not stages or k in stages]
    try:
        for n, key in enumerate(keys):
            st = design["stages"][key]
            if st["status"] in ("done", "skipped"):
                continue
            ctx.check()
            ctx.progress(n / len(keys), st["label"])
            ctx.stage(key, "running", st["label"])
            _set_stage(ctx, st, status="running", started=time.time())
            try:
                HANDLERS.get(key, _generic)(ctx, design, key, st, run_tests)
            except StepFailed as e:
                _set_stage(ctx, st, status="error", error=e.error)
                ctx.stage(key, "error", st["label"], error=e.error)
                raise
            if st["status"] == "running":
                _set_stage(ctx, st, status="done", finished=time.time())
            ctx.stage(key, st["status"], st["label"])
        with ctx.store.mutate():
            design["status"] = "done"
        ctx.state_changed("design")
        ctx.progress(1.0, "Programtervezés kész")
        return design
    except BaseException as e:
        with ctx.store.mutate():
            design["status"] = "cancelled" if ctx.job.cancel_token.is_set() else "error"
            design["error"] = getattr(e, "error", None) or {"message": str(e)}
            for st in design["stages"].values():
                if st["status"] == "running":
                    st["status"] = design["status"]
        ctx.state_changed("design")
        raise


# ------------------------------------------------------------------ handlers
def _stage_prompt(ctx: WorkflowContext, design: dict, key: str, slot: str, template: str | None = None) -> str:
    tpl = template or next(s[3] for s in STAGES if s[0] == key)
    budget = ctx.budget(slot, 0.8)
    files = ctx.store.snapshot()["code"]["files"]
    return ctx.fmt(tpl, requirements=ctx.clip_for(slot, design["requirements"], 0.2),
                   code=testing.code_listing(files, ctx.budget(slot, 0.55), tests=False),
                   review_issues=_issues_text([i for i in design["review_issues"] if i["status"] == "open"]),
                   code_rules=ctx.fmt(prompts.CODE_RULES), **_prev(design, budget))


def _system(ctx: WorkflowContext, design: dict, slot: str) -> str:
    return ctx.fmt(prompts.DEV_SYSTEM if slot == design["developer"] else prompts.REVIEWER_SYSTEM)


def _generic(ctx: WorkflowContext, design: dict, key: str, st: dict, _run_tests: bool) -> None:
    slot = _slot(design, st["actor"])
    msg = ctx.call(slot, _stage_prompt(ctx, design, key, slot), role="developer" if slot == design["developer"]
                   else "reviewer", stage=key, title=st["label"], system=_system(ctx, design, slot))
    data = extract_json(msg["content"])
    with ctx.store.mutate():
        msg["structured"] = data
    _set_stage(ctx, st, messages=st["messages"] + [msg["id"]], text=remove_json_blocks(msg["content"]) or msg["content"],
               data=data, by=slot)
    if key == "design_review":
        issues = _normalize_issues(data)
        _set_stage(ctx, st, issues=issues)


def _architecture(ctx: WorkflowContext, design: dict, key: str, st: dict, run_tests: bool) -> None:
    if not ctx.settings.get("dual_architecture", True):
        return _generic(ctx, design, key, st, run_tests)
    dev, rev = design["developer"], design["reviewer"]
    ctx.log("info", "Architektúra: mindkét modell önálló javaslatot tesz, majd közös döntés.")
    results = ctx.parallel({
        dev: lambda: ctx.call(dev, _stage_prompt(ctx, design, key, dev), role="developer", stage=key,
                              title="Architektúra-javaslat", system=_system(ctx, design, dev)),
        rev: lambda: ctx.call(rev, _stage_prompt(ctx, design, key, rev, prompts.DESIGN_ARCH_ALT), role="reviewer",
                              stage=key, title="Alternatív architektúra-javaslat", system=_system(ctx, design, rev)),
    })
    proposals = {s: (m or {}).get("content", "") for s, (m, e) in results.items() if e is None}
    msg_ids = [m["id"] for m, e in results.values() if e is None and m]
    if dev not in proposals:
        raise StepFailed(key, {"kind": "no_proposal", "label": "Hiányzó javaslat",
                               "message": f"A fejlesztő (LLM {dev}) nem adott architektúra-javaslatot."})
    text, data = proposals[dev], extract_json(proposals[dev])
    note = ""
    if rev in proposals:
        try:
            rec = run_consensus(ctx, task=design["requirements"], candidates=proposals,
                                source="design_architecture", synthesizer=dev)
            text = rec["merged"] or text
            data = extract_json(text) or data
            note = f"Közös döntés: {rec['id']}"
            st["consensus_id"] = rec["id"]
        except StepFailed as e:
            note = f"Közös döntés sikertelen ({e}); a fejlesztő javaslata marad."
            ctx.log("warning", note)
    _set_stage(ctx, st, messages=st["messages"] + msg_ids, text=remove_json_blocks(text) or text, data=data,
               note=note, proposals={s: clip(t, 20000) for s, t in proposals.items()}, by=dev)


def _code(ctx: WorkflowContext, design: dict, key: str, st: dict, _run_tests: bool) -> None:
    dev = design["developer"]
    prompt = _stage_prompt(ctx, design, key, dev)
    files: dict[str, str] = {}
    msg_ids = []
    for attempt in range(2):
        msg = ctx.call(dev, prompt, role="developer", stage=key, title=st["label"] + (" (újra)" if attempt else ""),
                       system=_system(ctx, design, dev))
        msg_ids.append(msg["id"])
        files = {p: c for n, c in extract_files(msg["content"], default_name="main.py").items()
                 if (p := safe_path(n)) and not is_test_file(p)}
        if files:
            break
        ctx.log("warning", "A válasz nem tartalmazott felismerhető kódfájlt – újrakérés FILE: formátumban.")
        prompt += "\n\nIMPORTANT: your previous answer had no code files. Output the files using the FILE: format."
    if not files:
        raise StepFailed(key, {"kind": "no_code", "label": "Nincs kód",
                               "message": "A fejlesztő modell nem adott felismerhető kódfájlt."})
    # A fresh implementation replaces previous non-test files.
    with ctx.store.lock:
        keep = {k: v for k, v in ctx.store.project["code"]["files"].items() if is_test_file(k)}
    version = ctx.store.set_code({**keep, **files}, source="code", note="Első implementáció", replace=True)
    ctx.state_changed("code")
    _set_stage(ctx, st, messages=st["messages"] + msg_ids, text=remove_json_blocks(msg["content"]),
               data={"files": sorted(files), "version": version["version"]}, by=dev)


def _code_review(ctx: WorkflowContext, design: dict, key: str, st: dict, _run_tests: bool) -> None:
    rev = design["reviewer"]
    msg, data = ctx.call_json(rev, _stage_prompt(ctx, design, key, rev), role="reviewer", stage=key,
                              title=st["label"], system=_system(ctx, design, rev), temperature=0.2)
    issues = _normalize_issues(data)
    with ctx.store.mutate():
        design["review_issues"] = issues
    verdict = (data or {}).get("verdict", "") if isinstance(data, dict) else ""
    _set_stage(ctx, st, messages=st["messages"] + [msg["id"]], text=remove_json_blocks(msg["content"]),
               data=data, issues=issues, verdict=verdict, by=rev)
    ctx.log("info", f"Kódellenőrzés: {len(issues)} probléma ({verdict or 'nincs verdikt'}).")


def _tests(ctx: WorkflowContext, design: dict, key: str, st: dict, _run_tests: bool) -> None:
    generated = testing.generate_tests(ctx, design["requirements"])
    t = ctx.store.snapshot()["testing"].get("generated") or {}
    _set_stage(ctx, st, messages=st["messages"] + t.get("messages", []),
               text="Generált tesztfájlok:\n" + "\n".join(f"- {n}" for n in sorted(generated)),
               data={"files": sorted(generated), "by_slot": t.get("by_slot", {})})


def _fix(ctx: WorkflowContext, design: dict, key: str, st: dict, run_tests: bool) -> None:
    dev = design["developer"]
    to_fix = [i for i in design["review_issues"] if i["status"] == "open" and i["severity"] in FIX_SEVERITIES]
    notes = []
    msg_ids = []
    if to_fix:
        msg = ctx.call(dev, _stage_prompt(ctx, design, key, dev), role="developer", stage=key,
                       title="Review-javítások", system=_system(ctx, design, dev))
        msg_ids.append(msg["id"])
        with ctx.store.lock:
            current = dict(ctx.store.project["code"]["files"])
        changed = {p: c for n, c in extract_files(msg["content"], default_name="").items()
                   if (p := safe_path(n)) and current.get(p) != c}
        if changed:
            v = ctx.store.set_code(changed, source="review_fix", note=f"{len(to_fix)} review-probléma javítása")
            with ctx.store.mutate():
                for i in to_fix:
                    i["status"] = "addressed"
            notes.append(f"Review-javítások alkalmazva (v{v['version']}): {', '.join(sorted(changed))}")
            ctx.state_changed("code")
        else:
            notes.append("A fejlesztő nem módosított fájlt a review alapján.")
    else:
        notes.append("Nem volt javítandó (critical/high/medium) review-probléma.")
    report = None
    if run_tests:
        if ctx.settings.get("allow_code_execution"):
            report = testing.fix_loop(ctx, design["requirements"])
            notes.append(testing.summary_text(report))
        else:
            notes.append("Tesztfuttatás kihagyva: a kódfuttatás nincs engedélyezve a beállításokban.")
            ctx.log("warning", notes[-1])
    _set_stage(ctx, st, messages=st["messages"] + msg_ids, text="\n".join(notes), data={"test_report": report})


def _final(ctx: WorkflowContext, design: dict, key: str, st: dict, _run_tests: bool) -> None:
    dev, rev = design["developer"], design["reviewer"]
    snap = ctx.store.snapshot()
    files = snap["code"]["files"]
    report = snap["testing"].get("report")
    tsum = testing.summary_text(report)
    open_issues = _issues_text([i for i in design["review_issues"] if i["status"] == "open"])
    readme_prompt = ctx.fmt(prompts.FINAL_README, requirements=ctx.clip_for(dev, design["requirements"], 0.3),
                            file_list="\n".join(f"- {n}" for n in sorted(files)), test_summary=tsum)
    audit_prompt = ctx.fmt(prompts.FINAL_AUDIT, requirements=ctx.clip_for(rev, design["requirements"], 0.15),
                           code=testing.code_listing(files, ctx.budget(rev, 0.55), tests=False),
                           test_summary=tsum, open_issues=open_issues)
    results = ctx.parallel({
        dev: lambda: ctx.call(dev, readme_prompt, role="developer", stage=key, title="Végleges dokumentáció",
                              system=_system(ctx, design, dev)),
        rev: lambda: ctx.call_json(rev, audit_prompt, role="reviewer", stage=key, title="Végső audit",
                                   system=_system(ctx, design, rev), temperature=0.2),
    })
    msg_ids = []
    readme_msg, err = results[dev]
    if err is None and readme_msg:
        msg_ids.append(readme_msg["id"])
        readme = readme_msg["content"].strip()
        if readme.startswith("```"):
            readme = readme.strip("`").split("\n", 1)[-1]
        ctx.store.set_code({"README.md": readme + "\n"}, source="final", note="Végleges dokumentáció")
        ctx.state_changed("code")
    audit = None
    res, err = results[rev]
    if err is None and res:
        amsg, audit = res
        msg_ids.append(amsg["id"])
    snap = ctx.store.snapshot()
    versions = snap["code"]["versions"]
    final = {"created": time.time(), "version": versions[-1]["version"] if versions else 0,
             "files": sorted(snap["code"]["files"]), "audit": audit if isinstance(audit, dict) else None,
             "test_report": report, "open_review_issues": [i for i in design["review_issues"] if i["status"] == "open"]}
    with ctx.store.mutate() as p:
        design["final"] = final
        if not p.get("final") or p["final"].get("source") != "pipeline":
            p["final"] = {"source": "design", **final}
    _set_stage(ctx, st, messages=st["messages"] + msg_ids,
               text=f"Végleges verzió: v{final['version']} ({len(final['files'])} fájl)\n{tsum}", data=final)


HANDLERS = {
    "architecture": _architecture,
    "code": _code,
    "code_review": _code_review,
    "tests": _tests,
    "fix": _fix,
    "final": _final,
}
