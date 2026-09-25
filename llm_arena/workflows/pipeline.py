"""Full flow: Task → analysis by both models → joint decision → debate →
collaborative design → code → tests → fixes → final solution."""

from __future__ import annotations

import time

from .. import prompts
from ..textutil import clip
from . import testing
from .arena import run_arena
from ..errors import describe_exception
from .common import StepFailed, WorkflowContext, message_text
from .consensus import run_consensus
from .debate import run_debate, synthesis_text
from .design import run_design

PIPELINE_STAGES = [
    ("analysis", "Elemzés (mindkét modell)"),
    ("consensus", "Közös döntés az elemzésekről"),
    ("debate", "Vita a megközelítésről"),
    ("design", "Közös tervezés → kód → teszt → javítás"),
    ("report", "Végleges megoldás és jelentés"),
]


def run_pipeline(ctx: WorkflowContext, *, task: str | None = None, resume: bool = False,
                 attachments: list[str] | None = None) -> dict:
    with ctx.store.mutate() as p:
        if not resume or not p.get("pipeline") or not p["pipeline"].get("task"):
            if not (task or "").strip():
                raise ValueError("Adj meg feladatot.")
            p["pipeline"] = {"task": task.strip(), "status": "running", "created": time.time(), "error": None,
                             "stages": {k: {"status": "pending", "label": lbl} for k, lbl in PIPELINE_STAGES},
                             "analysis_round": None, "consensus_id": None, "approach": "",
                             "attachments": list(attachments or [])}
            p["task"] = task.strip()
        pl = p["pipeline"]
        pl["status"] = "running"
        pl["error"] = None
    ctx.state_changed("pipeline")
    task = pl["task"]
    n = len(PIPELINE_STAGES)

    def mark(key: str, status: str, **extra) -> None:
        with ctx.store.mutate():
            pl["stages"][key].update(status=status, **extra)
        ctx.stage(key, status, pl["stages"][key]["label"])
        ctx.state_changed("pipeline")

    def pending(key: str) -> bool:
        return pl["stages"][key]["status"] not in ("done", "skipped")

    try:
        # 1. Both models analyse the task.
        if pending("analysis"):
            ctx.progress(0.02, "1/5 Elemzés – mindkét modell")
            mark("analysis", "running")
            res = run_arena(ctx, ctx.fmt(prompts.ANALYSIS_PROMPT, task=task), multi_turn=False, kind="analysis",
                            attachments=pl.get("attachments"))
            if not res["ok"]:
                raise StepFailed("analysis", {"kind": "analysis_failed", "label": "Elemzés sikertelen",
                                              "message": "Egyik modell sem adott elemzést."})
            with ctx.store.mutate():
                pl["analysis_round"] = res["round"]
            mark("analysis", "done", ok=res["ok"])

        # 2. Joint decision over the two analyses (or the single available one).
        if pending("consensus"):
            ctx.progress(0.12, "2/5 Közös döntés az elemzésekről")
            mark("consensus", "running")
            snap = ctx.store.snapshot()
            rnd = next(r for r in snap["arena"]["rounds"] if r["round"] == pl["analysis_round"])
            cands = {s: message_text(ctx.store, rnd["responses"].get(s)) for s in ("A", "B")}
            if all(cands.values()):
                rec = run_consensus(ctx, task=task, candidates=cands, source="pipeline_analysis")
                approach = rec["merged"]
                with ctx.store.mutate():
                    pl["consensus_id"] = rec["id"]
            else:
                approach = next(v for v in cands.values() if v)
                ctx.log("warning", "Csak egy modell elemzése érhető el – közös döntés kihagyva.")
            with ctx.store.mutate():
                pl["approach"] = approach
            mark("consensus", "done")

        # 3. Debate about the approach.
        if pending("debate"):
            ctx.progress(0.25, "3/5 Vita a megközelítésről")
            mark("debate", "running")
            topic = ctx.fmt(prompts.PIPELINE_TOPIC, task=task, approach=clip(pl["approach"], 6000))
            resume_debate = pl["stages"]["debate"].get("started", False)
            with ctx.store.mutate():
                pl["stages"]["debate"]["started"] = True
            run_debate(ctx, topic=topic, resume=resume_debate, attachments=pl.get("attachments"))
            mark("debate", "done")

        # 4. Collaborative design, code, tests, fixes.
        if pending("design"):
            ctx.progress(0.45, "4/5 Közös programtervezés")
            mark("design", "running")
            snap = ctx.store.snapshot()
            reqs = (f"{task}\n\nJOINTLY AGREED APPROACH:\n{clip(pl['approach'], 5000)}\n\n"
                    f"DEBATE CONCLUSIONS:\n{clip(synthesis_text(snap['debate']), 4000)}")
            resume_design = pl["stages"]["design"].get("started", False)
            with ctx.store.mutate():
                pl["stages"]["design"]["started"] = True
            run_design(ctx, requirements=reqs, resume=resume_design, attachments=pl.get("attachments"))
            mark("design", "done")

        # 5. Final report.
        if pending("report"):
            ctx.progress(0.95, "5/5 Végleges jelentés")
            mark("report", "running")
            build_final(ctx, task)
            mark("report", "done")
        with ctx.store.mutate():
            pl["status"] = "done"
        ctx.state_changed("pipeline")
        ctx.progress(1.0, "A teljes folyamat elkészült")
        return pl
    except BaseException as e:
        with ctx.store.mutate():
            pl["status"] = "cancelled" if ctx.job.cancel_token.is_set() else "error"
            pl["error"] = describe_exception(e)
            for st in pl["stages"].values():
                if st["status"] == "running":
                    st["status"] = pl["status"]
        ctx.state_changed("pipeline")
        raise


def build_final(ctx: WorkflowContext, task: str) -> dict:
    snap = ctx.store.snapshot()
    design = snap.get("design") or {}
    dfinal = design.get("final") or {}
    audit = dfinal.get("audit")
    stages = design.get("stages") or {}
    design_summary = "\n".join(f"{st['label']}: {clip(st.get('text') or '', 600)}"
                               for k, st in stages.items() if k in ("architecture", "modules", "algorithms"))
    slot = ctx.settings.get("moderator", "A")
    report_text = ""
    try:
        msg = ctx.call(slot, ctx.fmt(prompts.FINAL_REPORT, task=clip(task, 3000),
                                     debate=clip(synthesis_text(snap.get("debate") or {}), 3000),
                                     design=clip(design_summary, 3000),
                                     tests=testing.summary_text(snap["testing"].get("report")),
                                     audit=clip(str(audit or "-"), 2000)),
                       role="moderator", stage="final_report", title="Végleges jelentés")
        report_text = msg["content"]
    except StepFailed as e:
        ctx.log("warning", f"A végleges jelentés generálása sikertelen: {e}")
    final = {"source": "pipeline", "created": time.time(), "task": task, "report": report_text,
             "version": dfinal.get("version"), "files": sorted(snap["code"]["files"]),
             "audit": audit, "test_report": snap["testing"].get("report"),
             "debate_synthesis": (snap.get("debate") or {}).get("synthesis"),
             "consensus_id": (snap.get("pipeline") or {}).get("consensus_id")}
    with ctx.store.mutate() as p:
        p["final"] = final
    ctx.state_changed("final")
    return final
