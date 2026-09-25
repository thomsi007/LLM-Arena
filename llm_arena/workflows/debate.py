"""Structured multi-round debate: A = proponent, B = critic.

Besides the text transcript an explicit *ledger* is maintained (claims,
objections, concessions, questions, proposals per turn) and the final
synthesis is a JSON document: facts, disputed points, solutions, open
questions and a joint conclusion, cross-checked by the other model.
"""

from __future__ import annotations

import time
import uuid

from .. import prompts
from ..textutil import as_list, as_str_list, bullet, clip, extract_json, remove_json_blocks
from ..errors import describe_exception
from .common import StepFailed, WorkflowContext, other

ROLES = {"A": "proponent", "B": "critic"}
ROLE_HU = {"proponent": "Érvelő / javaslattevő", "critic": "Kritikus / ellenérvelő"}
PHASE_HU = {"position": "Álláspont", "critique": "Kritika", "rebuttal": "Válasz a kritikára",
            "counter": "Válasz az új érvekre"}
LEDGER_KEYS = ("claims", "objections", "concessions", "questions", "proposals")


def new_debate(topic: str, rounds: int, moderator: str, attachments: list[str] | None = None) -> dict:
    return {
        "attachments": list(attachments or []),
        "id": uuid.uuid4().hex[:10], "topic": topic.strip(), "rounds": max(1, min(int(rounds), 8)),
        "moderator": moderator, "status": "running", "created": time.time(), "error": None,
        "turns": [], "ledger": {k: [] for k in LEDGER_KEYS}, "synthesis": None, "synthesis_review": None,
    }


def plan(rounds: int) -> list[tuple[int, str, str]]:
    steps = []
    for r in range(1, rounds + 1):
        steps.append((r, "A", "position" if r == 1 else "rebuttal"))
        steps.append((r, "B", "critique" if r == 1 else "counter"))
    return steps


def _transcript(ctx: WorkflowContext, debate: dict, slot: str, share: float = 0.55) -> str:
    budget = ctx.budget(slot, share)
    parts: list[str] = []
    used = 0
    for t in reversed([t for t in debate["turns"] if t["status"] == "done"]):
        block = f"### LLM {t['slot']} – {ROLE_HU[t['role']]} – {t['round']}. kör, {PHASE_HU[t['phase']]}\n{t['content']}"
        if used + len(block) > budget:
            if not parts:
                parts.append(clip(block, budget))
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(reversed(parts)) or "(még nincs)"


def _state_text(debate: dict, limit: int = 8) -> str:
    led = debate["ledger"]

    def items(key: str, by: str | None = None) -> list[str]:
        sel = [i for i in led[key] if by is None or i["by"] == by]
        return [f"{i['text']} (LLM {i['by']}, {i['round']}. kör)" for i in sel[-limit:]]

    return "\n".join([
        "Proponent claims:\n" + bullet(items("claims", "A")),
        "Critic objections:\n" + bullet(items("objections", "B")),
        "Critic claims / alternatives:\n" + bullet(items("claims", "B") + items("proposals", "B")),
        "Concessions:\n" + bullet(items("concessions")),
        "Open questions:\n" + bullet(items("questions")),
    ])


def _record_turn(ctx: WorkflowContext, debate: dict, turn: dict, msg: dict) -> None:
    data = extract_json(msg["content"]) or {}
    structured = {k: as_str_list(data.get(k)) for k in LEDGER_KEYS}
    with ctx.store.mutate():
        turn.update(status="done", msg_id=msg["id"], content=remove_json_blocks(msg["content"]),
                    structured=structured, parsed=bool(data))
        msg["structured"] = structured
        for key, values in structured.items():
            for v in values:
                debate["ledger"][key].append({"text": v, "by": turn["slot"], "round": turn["round"],
                                              "turn": turn["index"]})


def run_debate(ctx: WorkflowContext, *, topic: str | None = None, rounds: int | None = None,
               resume: bool = False, attachments: list[str] | None = None) -> dict:
    settings = ctx.settings
    with ctx.store.mutate() as p:
        if not resume or not p.get("debate") or not p["debate"].get("topic"):
            if not (topic or "").strip():
                raise ValueError("Adj meg vitatémát.")
            p["debate"] = new_debate(topic, rounds or settings.get("debate_rounds", 2),
                                     settings.get("moderator", "A"), attachments)
            if not p["task"]:
                p["task"] = topic.strip()
        debate = p["debate"]
        debate["status"] = "running"
        debate["error"] = None
        # Drop unfinished turns from an interrupted run.
        debate["turns"] = [t for t in debate["turns"] if t["status"] == "done"]
    ctx.state_changed("debate")
    steps = plan(debate["rounds"])
    total = len(steps) + 2
    try:
        for i, (rnd, slot, phase) in enumerate(steps):
            if i < len(debate["turns"]):
                continue
            ctx.check()
            role = ROLES[slot]
            ctx.progress(i / total, f"Vita {rnd}. kör – LLM {slot}: {PHASE_HU[phase]}")
            turn = {"index": i, "round": rnd, "slot": slot, "role": role, "phase": phase,
                    "status": "running", "msg_id": None, "content": "", "structured": None}
            with ctx.store.mutate():
                debate["turns"].append(turn)
            instruction = ctx.fmt(prompts.DEBATE_PHASES[phase], round=rnd)
            files = ctx.files_text(slot, debate.get("attachments"), 0.25)
            user = ctx.fmt(prompts.DEBATE_TURN, topic=ctx.clip_for(slot, debate["topic"], 0.2)
                           + (f"\n\n{files}" if files else ""),
                           state=_state_text(debate), transcript=_transcript(ctx, debate, slot),
                           instruction=instruction)
            if rnd == 1 and debate.get("attachments"):
                # First round: send attached images as well (multimodal models).
                user = ctx.with_files(slot, user, [i for i in debate["attachments"]
                                                   if (ctx.attachments([i]) or [{}])[0].get("kind") == "image"])
            system = ctx.fmt(prompts.DEBATE_PROPONENT if slot == "A" else prompts.DEBATE_CRITIC)

            def attach(msg: dict, turn: dict = turn) -> None:
                turn["msg_id"] = msg["id"]

            try:
                msg = ctx.call(slot, user, role=role, stage=f"debate_{phase}", round=rnd,
                               title=f"Vita {rnd}. kör – {PHASE_HU[phase]}", system=system, on_created=attach,
                               tools=True)
            except StepFailed as e:
                with ctx.store.mutate():
                    turn.update(status="error", error=e.error)
                raise
            _record_turn(ctx, debate, turn, msg)
            ctx.state_changed("debate")
        synthesize(ctx, debate, total)
        with ctx.store.mutate():
            debate["status"] = "done"
        ctx.state_changed("debate")
        ctx.progress(1.0, "Vita kész")
        return debate
    except BaseException as e:
        with ctx.store.mutate():
            debate["status"] = "cancelled" if ctx.job.cancel_token.is_set() else "error"
            debate["error"] = describe_exception(e)
        ctx.state_changed("debate")
        raise


def synthesize(ctx: WorkflowContext, debate: dict, total: int) -> None:
    mod = debate.get("moderator") or "A"
    ctx.progress((total - 2) / total, f"Összegzés (moderátor: LLM {mod})")
    msg, data = ctx.call_json(
        mod, ctx.fmt(prompts.DEBATE_SYNTHESIS, topic=ctx.clip_for(mod, debate["topic"], 0.15),
                     state=_state_text(debate, 12), transcript=_transcript(ctx, debate, mod, 0.5)),
        role="moderator", stage="debate_synthesis", title="Vita összegzése", temperature=0.2)
    syn = normalize_synthesis(data)
    syn["msg_id"] = msg["id"]
    syn["parsed"] = data is not None
    if data is None:
        syn["conclusion"] = remove_json_blocks(msg["content"])
    with ctx.store.mutate():
        debate["synthesis"] = syn
    ctx.state_changed("debate")

    rv = other(mod)
    ctx.progress((total - 1) / total, f"Összegzés ellenőrzése (LLM {rv})")
    try:
        rmsg, rdata = ctx.call_json(
            rv, ctx.fmt(prompts.DEBATE_SYNTHESIS_REVIEW, topic=ctx.clip_for(rv, debate["topic"], 0.15),
                        summary=clip(_json(syn), ctx.budget(rv, 0.6))),
            role="reviewer", stage="debate_synthesis_review", title="Összegzés ellenőrzése", temperature=0.2)
    except StepFailed as e:
        ctx.log("warning", f"Az összegzés ellenőrzése kimaradt: {e}")
        return
    rdata = rdata if isinstance(rdata, dict) else {}
    review = {"by": rv, "msg_id": rmsg["id"], "agree": bool(rdata.get("agree", True)),
              "corrections": as_str_list(rdata.get("corrections")),
              "conclusion_comment": str(rdata.get("conclusion_comment") or "").strip()}
    with ctx.store.mutate():
        merge = [("facts", "missing_facts"), ("solutions", "additional_solutions"),
                 ("open_questions", "additional_questions")]
        for key, extra in merge:
            for item in as_str_list(rdata.get(extra)):
                if item not in syn[key]:
                    syn[key].append(item)
                    syn.setdefault("added_by_review", []).append({"field": key, "text": item})
        for item in as_str_list(rdata.get("missing_disputed")):
            syn["disputed"].append({"point": item, "proponent": "", "critic": ""})
        syn["corrections"] = review["corrections"]
        syn["conclusion_comment"] = review["conclusion_comment"]
        debate["synthesis_review"] = review
    ctx.state_changed("debate")


def normalize_synthesis(data) -> dict:
    data = data if isinstance(data, dict) else {}
    disputed = []
    for d in as_list(data.get("disputed")):
        if isinstance(d, dict):
            disputed.append({"point": str(d.get("point") or d.get("topic") or "").strip(),
                             "proponent": str(d.get("proponent") or d.get("a") or "").strip(),
                             "critic": str(d.get("critic") or d.get("b") or "").strip()})
        else:
            disputed.append({"point": str(d), "proponent": "", "critic": ""})
    return {
        "arguments_summary": as_str_list(data.get("arguments_summary")),
        "counterarguments_summary": as_str_list(data.get("counterarguments_summary")),
        "facts": as_str_list(data.get("facts")),
        "disputed": disputed,
        "solutions": as_str_list(data.get("solutions")),
        "open_questions": as_str_list(data.get("open_questions")),
        "conclusion": str(data.get("conclusion") or "").strip(),
    }


def synthesis_text(debate: dict) -> str:
    syn = (debate or {}).get("synthesis") or {}
    if not syn:
        return ""
    lines = ["Közös tények:", bullet(syn.get("facts")), "Vitatott pontok:",
             bullet([f"{d['point']} (A: {d['proponent']} | B: {d['critic']})" for d in syn.get("disputed", [])]),
             "Megoldási lehetőségek:", bullet(syn.get("solutions")), "Nyitott kérdések:",
             bullet(syn.get("open_questions")), "Közös következtetés:", syn.get("conclusion") or "-"]
    if syn.get("conclusion_comment"):
        lines.append(f"Megjegyzés: {syn['conclusion_comment']}")
    return "\n".join(lines)


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=1)
