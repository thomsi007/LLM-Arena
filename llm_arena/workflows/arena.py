"""Arena mode: the same prompt goes to both models in parallel, multi-round."""

from __future__ import annotations

import time

from .. import prompts
from ..providers import Cancelled
from .common import SLOTS, StepFailed, WorkflowContext


def _history(ctx: WorkflowContext, slot: str, upto_round: int) -> list[dict]:
    """Previous rounds as chat history for ``slot`` (only successful answers)."""
    snap = ctx.store.snapshot()
    msgs = {m["id"]: m for m in snap["messages"]}
    history: list[dict] = []
    for rnd in snap["arena"]["rounds"]:
        if rnd["round"] >= upto_round:
            break
        m = msgs.get(rnd["responses"].get(slot) or "")
        if not m or m.get("status") != "done":
            continue
        history += [{"role": "user", "content": rnd["prompt"]},
                    {"role": "assistant", "content": m["content"]}]
    # Keep the most recent turns inside the prompt budget.
    budget = ctx.budget(slot, 0.7)
    while history and sum(len(h["content"]) for h in history) > budget:
        history = history[2:]
    return history


def _ask(ctx: WorkflowContext, round_no: int, slot: str, prompt: str, multi_turn: bool) -> dict:
    history = _history(ctx, slot, round_no) if multi_turn else []

    def attach(msg: dict) -> None:
        for rnd in ctx.store.project["arena"]["rounds"]:
            if rnd["round"] == round_no:
                rnd["responses"][slot] = msg["id"]

    return ctx.call(slot, history + [{"role": "user", "content": prompt}], role="contestant",
                    stage="arena", round=round_no, title=f"Aréna {round_no}. kör",
                    system=ctx.fmt(prompts.ARENA_SYSTEM), on_created=attach)


def run_arena(ctx: WorkflowContext, prompt: str, *, multi_turn: bool = True,
              slots: tuple[str, ...] = SLOTS, kind: str = "arena") -> dict:
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Üres feladat.")
    with ctx.store.mutate() as p:
        round_no = len(p["arena"]["rounds"]) + 1
        p["arena"]["rounds"].append({"round": round_no, "prompt": prompt, "created": time.time(),
                                     "kind": kind, "multi_turn": multi_turn, "responses": {}})
        if not p["task"]:
            p["task"] = prompt
    ctx.state_changed("arena")
    ctx.progress(0.05, f"Aréna {round_no}. kör – {' és '.join(slots)} dolgozik")
    results = ctx.parallel({s: (lambda s=s: _ask(ctx, round_no, s, prompt, multi_turn)) for s in slots})
    errors = {s: e for s, (_, e) in results.items() if e is not None}
    for s, e in errors.items():
        if not isinstance(e, (StepFailed, Cancelled)):
            ctx.log("error", f"LLM {s}: váratlan hiba: {e}")
    ctx.state_changed("arena")
    ok = [s for s in slots if s not in errors]
    ctx.progress(1.0, f"Kész – sikeres: {', '.join(ok) or 'egyik sem'}")
    return {"round": round_no, "ok": ok, "failed": list(errors)}


def retry_slot(ctx: WorkflowContext, round_no: int, slot: str) -> dict:
    snap = ctx.store.snapshot()
    rnd = next((r for r in snap["arena"]["rounds"] if r["round"] == round_no), None)
    if not rnd:
        raise ValueError(f"Nincs {round_no}. kör.")
    ctx.progress(0.1, f"Újrapróbálás: LLM {slot}, {round_no}. kör")
    try:
        _ask(ctx, round_no, slot, rnd["prompt"], rnd.get("multi_turn", True))
    finally:
        ctx.state_changed("arena")
    return {"round": round_no, "slot": slot}
