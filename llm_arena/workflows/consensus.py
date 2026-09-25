"""Joint decision mechanism.

No winner/loser: both models score *both* candidate solutions (anonymised) on
six criteria, the scores are aggregated per criterion, the best ideas of both
sides are collected and a combined solution is written and cross-reviewed.
"""

from __future__ import annotations

import random
import time
import uuid

from .. import prompts
from ..textutil import as_list, as_str_list, bullet
from .common import SLOTS, StepFailed, WorkflowContext, other


def _score(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(10.0, f))


def aggregate(evaluations: dict[str, dict], mapping: dict[str, dict[str, str]]) -> dict:
    """Combine evaluations. ``mapping[evaluator]`` maps anonymous label -> real slot."""
    per: dict[str, dict[str, list[float]]] = {s: {c: [] for c in prompts.CRITERIA} for s in SLOTS}
    strengths: dict[str, list[str]] = {s: [] for s in SLOTS}
    weaknesses: dict[str, list[str]] = {s: [] for s in SLOTS}
    ideas: list[dict] = []
    conflicts: list[str] = []
    for evaluator, data in evaluations.items():
        if not isinstance(data, dict):
            continue
        labels = mapping[evaluator]
        scores = data.get("scores") or {}
        for label, slot in labels.items():
            sc = scores.get(label) or scores.get(f"solution {label}") or {}
            if isinstance(sc, dict):
                for c in prompts.CRITERIA:
                    v = _score(sc.get(c))
                    if v is not None:
                        per[slot][c].append(v)
            strengths[slot] += as_str_list((data.get("strengths") or {}).get(label))
            weaknesses[slot] += as_str_list((data.get("weaknesses") or {}).get(label))
        for idea in as_list(data.get("best_ideas")):
            if isinstance(idea, dict):
                src = labels.get(str(idea.get("from", "")).strip().replace("Solution ", ""), "?")
                text = str(idea.get("idea") or "").strip()
            else:
                src, text = "?", str(idea).strip()
            if text:
                ideas.append({"from": src, "idea": text, "evaluator": evaluator})
        conflicts += as_str_list(data.get("conflicts"))

    def dedupe(items):
        seen, out = set(), []
        for it in items:
            key = (it["from"], it["idea"].lower()) if isinstance(it, dict) else str(it).lower()
            if key not in seen:
                seen.add(key)
                out.append(it)
        return out

    strengths = {s: dedupe(v) for s, v in strengths.items()}
    weaknesses = {s: dedupe(v) for s, v in weaknesses.items()}
    ideas = dedupe(ideas)
    conflicts = dedupe(conflicts)
    table = {}
    leaders = {}
    for c in prompts.CRITERIA:
        row = {}
        for s in SLOTS:
            vals = per[s][c]
            row[s] = round(sum(vals) / len(vals), 2) if vals else None
        table[c] = row
        a, b = row["A"], row["B"]
        if a is None and b is None:
            leaders[c] = None
        elif b is None or (a is not None and a - b > 0.5):
            leaders[c] = "A"
        elif a is None or (b - a > 0.5):
            leaders[c] = "B"
        else:
            leaders[c] = "tie"
    totals = {}
    for s in SLOTS:
        vals = [table[c][s] for c in prompts.CRITERIA if table[c][s] is not None]
        totals[s] = round(sum(vals) / len(vals), 2) if vals else None
    return {"table": table, "leaders": leaders, "averages": totals, "strengths": strengths,
            "weaknesses": weaknesses, "best_ideas": ideas, "conflicts": conflicts}


def run_consensus(ctx: WorkflowContext, *, task: str, candidates: dict[str, str], source: str,
                  synthesizer: str | None = None, review: bool = True) -> dict:
    """Evaluate two candidates and build a combined solution. Returns the consensus record."""
    synthesizer = synthesizer or ctx.settings.get("moderator", "A")
    if not all(candidates.get(s, "").strip() for s in SLOTS):
        raise ValueError("A közös döntéshez mindkét modell sikeres válasza szükséges.")
    rec = {"id": uuid.uuid4().hex[:10], "created": time.time(), "source": source, "task": task,
           "status": "running", "evaluations": {}, "messages": [], "aggregate": None,
           "merged": "", "review": None, "synthesizer": synthesizer}
    with ctx.store.mutate() as p:
        p["consensus"].append(rec)
    ctx.state_changed("consensus")

    # 1) Cross-evaluation, anonymised with random order per evaluator (reduces self-preference).
    mapping: dict[str, dict[str, str]] = {}
    calls = {}
    for ev in SLOTS:
        order = list(SLOTS)
        random.shuffle(order)
        mapping[ev] = {"1": order[0], "2": order[1]}
        text = ctx.fmt(prompts.CONSENSUS_EVAL, task=ctx.clip_for(ev, task, 0.15),
                       sol1=ctx.clip_for(ev, candidates[order[0]], 0.35),
                       sol2=ctx.clip_for(ev, candidates[order[1]], 0.35))
        calls[ev] = (lambda ev=ev, text=text: ctx.call_json(
            ev, text, role="evaluator", stage="consensus_eval", title="Közös értékelés",
            system=ctx.fmt(prompts.CONSENSUS_SYSTEM), temperature=0.2))
    ctx.progress(0.1, "Kereszt-értékelés (mindkét modell mindkét megoldást pontozza)")
    results = ctx.parallel(calls)
    evaluations = {}
    for ev, (res, err) in results.items():
        if err is None and res:
            msg, data = res
            with ctx.store.mutate():
                rec["messages"].append(msg["id"])
            if isinstance(data, dict):
                evaluations[ev] = data
        elif err is not None:
            ctx.log("warning", f"LLM {ev} értékelése sikertelen: {err}")
    if not evaluations:
        with ctx.store.mutate():
            rec["status"] = "error"
        ctx.state_changed("consensus")
        raise StepFailed("consensus_eval", {"kind": "no_evaluation", "label": "Értékelés sikertelen",
                                            "message": "Egyik modell sem adott értelmezhető értékelést."})
    agg = aggregate(evaluations, mapping)
    with ctx.store.mutate():
        rec["evaluations"] = {ev: {"mapping": mapping[ev], "data": d} for ev, d in evaluations.items()}
        rec["aggregate"] = agg
    ctx.state_changed("consensus")

    # 2) Combined solution.
    ctx.progress(0.5, f"Közös megoldás összeállítása (LLM {synthesizer})")
    leaders = ", ".join(f"{prompts.CRITERIA_HU[c]}: {agg['leaders'][c] or '-'}" for c in prompts.CRITERIA)
    ideas = bullet([f"[{i['from']}] {i['idea']}" for i in agg["best_ideas"]])
    weak = bullet([f"[A] {w}" for w in agg["weaknesses"]["A"]] + [f"[B] {w}" for w in agg["weaknesses"]["B"]])
    merge_prompt = ctx.fmt(prompts.CONSENSUS_MERGE, task=ctx.clip_for(synthesizer, task, 0.15),
                           sol_a=ctx.clip_for(synthesizer, candidates["A"], 0.3),
                           sol_b=ctx.clip_for(synthesizer, candidates["B"], 0.3),
                           leaders=leaders, ideas=ideas, weaknesses=weak, conflicts=bullet(agg["conflicts"]))
    merged_msg = ctx.call(synthesizer, merge_prompt, role="synthesizer", stage="consensus_merge",
                          title="Közös megoldás", system=ctx.fmt(prompts.ARENA_SYSTEM))
    merged = merged_msg["content"]
    with ctx.store.mutate():
        rec["messages"].append(merged_msg["id"])
        rec["merged"] = merged
    ctx.state_changed("consensus")

    # 3) Cross-review by the other model, one revision if needed.
    if review:
        rv = other(synthesizer)
        ctx.progress(0.75, f"Közös megoldás ellenőrzése (LLM {rv})")
        try:
            rmsg, rdata = ctx.call_json(rv, ctx.fmt(prompts.CONSENSUS_REVIEW, task=ctx.clip_for(rv, task, 0.15),
                                                    merged=ctx.clip_for(rv, merged, 0.6)),
                                        role="reviewer", stage="consensus_review", title="Közös megoldás review",
                                        system=ctx.fmt(prompts.CONSENSUS_SYSTEM), temperature=0.2)
            with ctx.store.mutate():
                rec["messages"].append(rmsg["id"])
            rdata = rdata if isinstance(rdata, dict) else {}
            issues = as_str_list(rdata.get("remaining_issues"))
            with ctx.store.mutate():
                rec["review"] = {"by": rv, "approved": bool(rdata.get("approved", not issues)),
                                 "remaining_issues": issues, "improvements": as_str_list(rdata.get("improvements"))}
            if issues and not rdata.get("approved", False):
                ctx.progress(0.9, "Közös megoldás javítása a review alapján")
                rev_msg = ctx.call(synthesizer, ctx.fmt(prompts.CONSENSUS_REVISE, merged=merged,
                                                        issues=bullet(issues),
                                                        improvements=bullet(rec["review"]["improvements"])),
                                   role="synthesizer", stage="consensus_revise", title="Közös megoldás – javított")
                with ctx.store.mutate():
                    rec["messages"].append(rev_msg["id"])
                    rec["merged"] = rev_msg["content"]
                    rec["review"]["revised"] = True
        except StepFailed as e:
            ctx.log("warning", f"A közös megoldás review-ja kimaradt: {e}")
    with ctx.store.mutate():
        rec["status"] = "done"
    ctx.state_changed("consensus")
    ctx.progress(1.0, "Közös döntés kész")
    return rec
