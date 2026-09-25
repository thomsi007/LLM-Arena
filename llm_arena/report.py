"""Self-contained, styled HTML export of project sections.

Every export is a single .html file (inline CSS, no scripts, no external
resources) that follows the reader's light/dark preference and prints
cleanly to PDF. All model output is HTML-escaped before markdown rendering.
"""

from __future__ import annotations

import html
import re
import time
from typing import Callable

from .prompts import CRITERIA, CRITERIA_HU

SECTIONS = {
    "arena": "Aréna",
    "debate": "Vita",
    "design": "Közös programtervezés",
    "testing": "Automatikus tesztelés",
    "consensus": "Közös döntés",
    "code": "Kód",
    "pipeline": "Teljes folyamat",
    "all": "Teljes riport",
}

PHASE_HU = {"position": "Álláspont", "critique": "Kritika", "rebuttal": "Válasz a kritikára",
            "counter": "Válasz az új érvekre"}
ROLE_HU = {"proponent": "Érvelő / javaslattevő", "critic": "Kritikus / ellenérvelő"}
STATUS_HU = {"done": "kész", "running": "fut", "error": "hiba", "cancelled": "megszakítva", "pending": "várakozik",
             "passed": "sikeres", "failed": "bukott", "timeout": "időtúllépés", "skipped": "kihagyva",
             "open": "nyitott", "addressed": "javítva", "no_change": "nincs változás", "streaming": "fut"}
CAT_HU = {"unit": "Unit", "integration": "Integrációs", "edge_case": "Edge-case",
          "error_handling": "Hibakezelés", "performance": "Teljesítmény"}


def e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


# --------------------------------------------------------------------------- #
# Markdown (escape first, then a safe subset)
# --------------------------------------------------------------------------- #

def _inline(s: str) -> str:
    codes: list[str] = []

    def keep(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`\n]+)`", keep, s)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__([^_\n]+)__", r"<strong>\1</strong>", s)
    s = re.sub(r"(^|[^*\w])\*([^*\n]+)\*(?!\w)", r"\1<em>\2</em>", s)
    s = re.sub(r"~~([^~\n]+)~~", r"<del>\1</del>", s)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" rel="noopener noreferrer">\1</a>', s)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", s)


def markdown(src: str) -> str:
    text = e(src or "").replace("\r\n", "\n")
    blocks: list[tuple[str, str]] = []

    def fence(m):
        blocks.append((m.group(1).strip(), m.group(2).rstrip("\n")))
        return f"\n\x01{len(blocks) - 1}\x01\n"

    text = re.sub(r"```([^\n`]*)\n(.*?)(?:```|$)", fence, text, flags=re.S)
    lines = text.split("\n")
    out: list[str] = []
    para: list[str] = []

    def flush():
        if para:
            out.append(f"<p>{_inline('<br>'.join(para))}</p>")
            para.clear()

    i = 0
    item_re = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\x01(\d+)\x01$", line)
        if m:
            flush()
            info, code = blocks[int(m.group(1))]
            label = f'<span class="lang">{info}</span>' if info else ""
            out.append(f'<pre class="code">{label}<code>{code}</code></pre>')
            i += 1
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            lvl = min(6, len(m.group(1)) + 2)  # nest below the document's own headings
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^\s*(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$", line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if line.startswith("&gt;"):
            flush()
            q = []
            while i < len(lines) and lines[i].startswith("&gt;"):
                q.append(re.sub(r"^&gt;\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{_inline('<br>'.join(q))}</blockquote>")
            continue
        if re.match(r"^\s*\|.*\|\s*$", line) and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            flush()

            def row(l):
                return [_inline(c.strip()) for c in l.strip().strip("|").split("|")]
            head = row(line)
            i += 2
            rows = []
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                rows.append(row(lines[i]))
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
                       + "</tbody></table>")
            continue
        m = item_re.match(line)
        if m:
            flush()
            ordered = m.group(2)[0].isdigit()
            items = []
            while i < len(lines) and (m := item_re.match(lines[i])):
                cls = ' class="sub"' if len(m.group(1)) >= 2 else ""
                item = m.group(3)
                i += 1
                while i < len(lines) and re.match(r"^\s{2,}\S", lines[i]) and not item_re.match(lines[i]):
                    item += " " + lines[i].strip()
                    i += 1
                items.append(f"<li{cls}>{_inline(item)}</li>")
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        para.append(line)
        i += 1
    flush()
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #

def fmt_ts(ts) -> str:
    return time.strftime("%Y. %m. %d. %H:%M", time.localtime(ts)) if ts else "–"


def fmt_sec(s) -> str:
    if s is None:
        return "–"
    return f"{s * 1000:.0f} ms" if s < 1 else f"{s:.2f} s"


def badge(text, kind: str = "") -> str:
    return f'<span class="badge {e(kind)}">{e(text)}</span>'


def status(s) -> str:
    return badge(STATUS_HU.get(s, s), s)


def ul(items, empty: str = "–") -> str:
    items = [i for i in (items or []) if i]
    if not items:
        return f'<p class="muted">{e(empty)}</p>'
    return "<ul>" + "".join(f"<li>{e(i if isinstance(i, str) else str(i))}</li>" for i in items) + "</ul>"


class Ctx:
    def __init__(self, project: dict):
        self.p = project
        self.msgs = {m["id"]: m for m in project.get("messages", [])}

    def name(self, slot: str) -> str:
        return (self.p.get("llms", {}).get(slot) or {}).get("name") or f"LLM {slot}"

    def msg(self, msg_id):
        return self.msgs.get(msg_id or "")


def files_line(ctx: Ctx, ids) -> str:
    if not ids:
        return ""
    names = {a["id"]: a for a in ctx.p.get("attachments", []) if isinstance(a, dict)}
    items = [e(names[i]["name"]) if i in names else "törölt fájl" for i in ids]
    return f'<p class="muted">📎 Csatolt fájlok: {", ".join(items)}</p>'


def message_card(ctx: Ctx, m: dict | None, title: str = "", content: str | None = None, slot: str = "") -> str:
    if not m:
        return (f'<article class="msg slot-{e(slot)}"><header>{badge("LLM " + slot, slot.lower())}'
                f'<span class="muted">nincs válasz</span></header></article>')
    tok = f"{m.get('tokens') or 0}{' (becsült)' if m.get('tokens_estimated') else ''} token"
    meta = [f"⏱ {fmt_sec(m.get('latency'))}"]
    if m.get("ttft") is not None:
        meta.append(f"első token {fmt_sec(m['ttft'])}")
    meta.append(tok)
    if m.get("prompt_tokens") is not None:
        meta.append(f"{m['prompt_tokens']} prompt token")
    if m.get("tokens_per_second"):
        meta.append(f"{m['tokens_per_second']} tok/s")
    err = m.get("error")
    body = markdown(content if content is not None else m.get("content", ""))
    reasoning = ""
    if m.get("reasoning"):
        reasoning = (f'<details class="reasoning"><summary>Gondolatmenet ({len(m["reasoning"])} karakter)</summary>'
                     f'<pre>{e(m["reasoning"])}</pre></details>')
    tools = ""
    if m.get("tool_calls"):
        rows = []
        for c in m["tool_calls"]:
            arg = (c.get("arguments") or {}).get("query") or (c.get("arguments") or {}).get("url") or ""
            label = "🔎 Webes keresés" if c.get("name") == "web_search" else "🌐 Oldal olvasása"
            src = "".join(f'<li><a href="{e(s["url"])}" rel="noopener noreferrer">{e(s.get("title") or s["url"])}</a></li>'
                          for s in (c.get("sources") or [])[:6] if str(s.get("url", "")).startswith(("http://", "https://")))
            rows.append(f'<div class="tool"><b>{label}</b> <code>{e(arg)}</code> <span class="muted">{e(c.get("summary"))}</span>'
                        f'{"<ol>" + src + "</ol>" if src else ""}</div>')
        tools = f'<details class="tools" open><summary>{len(m["tool_calls"])} webes eszközhívás</summary>{"".join(rows)}</details>'
    err_html = (f'<div class="error"><strong>{e(err.get("label", "Hiba"))}</strong>: {e(err.get("message"))}</div>'
                if err else "")
    return (f'<article class="msg slot-{e(m["slot"])}">'
            f'<header>{badge("LLM " + m["slot"], m["slot"].lower())}<strong>{e(title or m.get("title") or ctx.name(m["slot"]))}</strong>'
            f'<span class="model">{e(m.get("model"))}</span><span class="grow"></span>{status(m.get("status"))}</header>'
            f'<div class="body">{tools}{reasoning}{body or "<p class=muted>(üres)</p>"}{err_html}</div>'
            f'<footer>{" · ".join(e(x) for x in meta)}</footer></article>')


def score_bar(slot: str, v) -> str:
    width = 0 if v is None else max(0.0, min(10.0, float(v))) * 10
    return (f'<div class="score {slot.lower()}"><span>{e("–" if v is None else v)}</span>'
            f'<div class="bar"><i style="width:{width:.0f}%"></i></div></div>')


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #

def sec_arena(ctx: Ctx) -> str:
    rounds = ctx.p.get("arena", {}).get("rounds") or []
    if not rounds:
        return '<p class="muted">Nincs aréna kör.</p>'
    out = []
    for r in rounds:
        a, b = ctx.msg(r["responses"].get("A")), ctx.msg(r["responses"].get("B"))
        cmp = ""
        if a and b and a.get("status") == "done" and b.get("status") == "done":
            faster = "A" if a["latency"] <= b["latency"] else "B"
            cmp = (f'<p class="muted">Gyorsabb: LLM {faster} ({fmt_sec(abs(a["latency"] - b["latency"]))} különbség) · '
                   f'tokenek: A {a.get("tokens")} / B {b.get("tokens")}</p>')
        kind = " · elemzés" if r.get("kind") == "analysis" else ""
        out.append(f'<section class="round"><h3>{r["round"]}. kör{kind} <span class="muted">{fmt_ts(r.get("created"))}</span></h3>'
                   f'<div class="prompt">{markdown(r["prompt"])}{files_line(ctx, r.get("attachments"))}</div>'
                   f'<div class="versus">{message_card(ctx, a, slot="A", title=ctx.name("A"))}'
                   f'{message_card(ctx, b, slot="B", title=ctx.name("B"))}</div>{cmp}</section>')
    return "".join(out)


def synthesis_html(d: dict) -> str:
    s = d.get("synthesis")
    if not s:
        return '<p class="muted">Nincs összegzés.</p>'
    disputed = s.get("disputed") or []
    table = ("<table><thead><tr><th>Vitatott pont</th><th>LLM A (érvelő)</th><th>LLM B (kritikus)</th></tr></thead><tbody>"
             + "".join(f"<tr><td>{e(x.get('point'))}</td><td>{e(x.get('proponent'))}</td><td>{e(x.get('critic'))}</td></tr>"
                       for x in disputed) + "</tbody></table>") if disputed else '<p class="muted">Nincs vitatott pont.</p>'
    rv = d.get("synthesis_review") or {}
    return (f'<div class="panel highlight"><div class="grid2"><div><h4>Érvek (A)</h4>{ul(s.get("arguments_summary"))}</div>'
            f'<div><h4>Ellenérvek (B)</h4>{ul(s.get("counterarguments_summary"))}</div></div>'
            f'<h4>✅ Közös ténylista</h4>{ul(s.get("facts"))}<h4>⚡ Vitatott pontok</h4>{table}'
            f'<div class="grid2"><div><h4>💡 Megoldási lehetőségek</h4>{ul(s.get("solutions"))}</div>'
            f'<div><h4>❓ Nyitott kérdések</h4>{ul(s.get("open_questions"))}</div></div>'
            f'<h4>🤝 Közös következtetés</h4><div class="conclusion">{markdown(s.get("conclusion") or "–")}</div>'
            + (f'<p class="muted">LLM {e(rv.get("by"))} megjegyzése: {e(s.get("conclusion_comment"))}</p>'
               if s.get("conclusion_comment") else "")
            + (f'<h4>Javítások az ellenőrzésből</h4>{ul(s.get("corrections"))}' if s.get("corrections") else "")
            + "</div>")


def sec_debate(ctx: Ctx) -> str:
    d = ctx.p.get("debate") or {}
    if not d.get("topic"):
        return '<p class="muted">Nincs vita.</p>'
    led = d.get("ledger") or {}

    def count(k, by=None):
        return len([i for i in led.get(k, []) if by is None or i.get("by") == by])
    stats = stat_row([(count("claims", "A"), "A érvei"), (count("objections", "B"), "B kifogásai"),
                      (count("concessions"), "Elfogadott pontok"), (count("questions"), "Nyitott kérdések"),
                      (count("proposals"), "Javaslatok")])
    turns = []
    for t in d.get("turns", []):
        chips = "".join(f'<span class="chip {k}">{label}: {e(v)}</span>'
                        for k, label in (("claims", "érv"), ("objections", "kifogás"), ("concessions", "elfogadott"),
                                         ("questions", "kérdés"), ("proposals", "javaslat"))
                        for v in (t.get("structured") or {}).get(k, []))
        title = f"{t['round']}. kör · {PHASE_HU.get(t['phase'], t['phase'])} · {ROLE_HU.get(t['role'], t['role'])}"
        turns.append(f'<div class="turn slot-{e(t["slot"])}">'
                     f'{message_card(ctx, ctx.msg(t.get("msg_id")), title=title, content=t.get("content") or None, slot=t["slot"])}'
                     f'{"<div class=chips>" + chips + "</div>" if chips else ""}</div>')
    return (f'<div class="panel"><h4>Vitatéma</h4>{markdown(d["topic"])}'
            f'<p class="muted">{d.get("rounds")} kör · moderátor: LLM {e(d.get("moderator"))} · állapot: {status(d.get("status"))}</p></div>'
            f'<h3>Strukturált állapot</h3>{stats}<h3>Vita menete</h3><div class="timeline">{"".join(turns)}</div>'
            f'<h3>Vita eredménye</h3>{synthesis_html(d)}')


def issues_table(issues) -> str:
    if not issues:
        return '<p class="muted">Nincs rögzített probléma.</p>'
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows = sorted(issues, key=lambda i: order.get(i.get("severity"), 9))
    return ("<table><thead><tr><th>ID</th><th>Súlyosság</th><th>Kategória</th><th>Hely</th><th>Leírás</th>"
            "<th>Javaslat</th><th>Állapot</th></tr></thead><tbody>"
            + "".join(f"<tr><td class=mono>{e(i.get('id'))}</td><td>{badge(i.get('severity'), i.get('severity'))}</td>"
                      f"<td>{e(i.get('category'))}</td><td class=mono>{e(i.get('location'))}</td><td>{e(i.get('description'))}</td>"
                      f"<td>{e(i.get('suggestion'))}</td><td>{status(i.get('status', 'open'))}</td></tr>" for i in rows)
            + "</tbody></table>")


def sec_design(ctx: Ctx) -> str:
    d = ctx.p.get("design") or {}
    if not d.get("requirements"):
        return '<p class="muted">Nincs programterv.</p>'
    parts = [f'<div class="panel"><h4>Követelmények</h4>{markdown(d["requirements"])}'
             f'<p class="muted">Fejlesztő: LLM {e(d.get("developer"))} · reviewer: LLM {e(d.get("reviewer"))} · '
             f'állapot: {status(d.get("status"))}</p></div>']
    final = d.get("final")
    if final:
        tr = final.get("test_report") or {}
        parts.append(stat_row([(f"v{final.get('version')}", "Végleges verzió"),
                               (len(final.get("files") or []), "Fájl"),
                               (tr.get("passed", "–"), "Sikeres teszt"), (tr.get("failed", "–"), "Sikertelen teszt"),
                               ("✔" if (final.get("audit") or {}).get("approved") else "–", "Végső audit")]))
    for key in d.get("order", []):
        st = d["stages"][key]
        who = {"dev": f"fejlesztő (LLM {d.get('developer')})", "rev": f"reviewer (LLM {d.get('reviewer')})"}.get(
            st.get("actor"), "mindkét modell")
        extra = f"<h5>Talált problémák</h5>{issues_table(st['issues'])}" if st.get("issues") else ""
        parts.append(f'<section class="stage"><h3>{e(st["label"])} {status(st["status"])} <span class="muted">{e(who)}</span></h3>'
                     f'{markdown(st.get("text") or "") or "<p class=muted>–</p>"}{extra}</section>')
    parts.append(f"<h3>Review problémák (kódellenőrzés)</h3>{issues_table(d.get('review_issues'))}")
    return "".join(parts)


def stat_row(items) -> str:
    return '<div class="stats">' + "".join(f'<div class="stat"><b>{e(v)}</b><span>{e(k)}</span></div>'
                                           for v, k in items) + "</div>"


def sec_testing(ctx: Ctx) -> str:
    t = ctx.p.get("testing") or {}
    rep = t.get("report")
    runs = t.get("runs") or []
    if not rep and not runs:
        return '<p class="muted">Nem futottak tesztek.</p>'
    out = []
    if rep:
        out.append(stat_row([(rep["passed"], "Sikeres teszt"), (rep["failed"], "Sikertelen teszt"),
                             (len(rep["fixed_bugs"]), "Javított hiba"), (len(rep["remaining_issues"]), "Fennmaradó probléma"),
                             (f"{rep['iterations']}/{rep['max_iterations']}", "Javító iteráció"),
                             (f"v{rep['final_version']}", "Végleges kód")]))
        cats = rep.get("by_category") or {}
        if cats:
            out.append("<p>" + " ".join(badge(f"{CAT_HU.get(k, k)}: {v['passed']}/{v['total']}",
                                              "passed" if v["passed"] == v["total"] else "failed")
                                        for k, v in cats.items()) + "</p>")
        if rep["fixed_bugs"]:
            out.append("<h3>Javított hibák</h3><table><thead><tr><th>Teszt</th><th>Iteráció</th><th>Gyökérok</th>"
                       "<th>Javítás helye</th><th>Kódverzió</th></tr></thead><tbody>"
                       + "".join(f"<tr><td class=mono>{e(b['test'])}</td><td>{b['iteration']}</td><td>{e(b.get('root_cause'))}</td>"
                                 f"<td>{e(b.get('fix_target'))}</td><td>v{b.get('code_version')}</td></tr>" for b in rep["fixed_bugs"])
                       + "</tbody></table>")
        out.append(f"<h3>Fennmaradó problémák</h3>{ul(rep['remaining_issues'], 'Nincs – minden teszt sikeres.')}")
    if runs:
        last = runs[-1]
        def dur(x):
            return "" if x.get("duration") is None else f"{x['duration'] * 1000:.0f} ms"
        rows = "".join(
            f"<tr><td>{status(x['status'])}</td><td>{e(CAT_HU.get(x['category'], x['category']))}</td>"
            f"<td class=mono>{e(x['id'])}</td><td>{dur(x)}</td>"
            f"<td>{e(x.get('message'))}</td></tr>"
            + (f"<tr class=tb><td colspan=5><pre>{e(x['traceback'])}</pre></td></tr>"
               if x["status"] in ("failed", "error", "timeout") and x.get("traceback") else "")
            for x in last.get("tests", []))
        out.append(f"<h3>Legutóbbi futtatás <span class='muted'>{fmt_ts(last.get('created'))} · kód v{last.get('code_version')} · "
                   f"{last.get('duration')} s</span></h3>"
                   + (f"<table><thead><tr><th>Állapot</th><th>Kategória</th><th>Teszt</th><th>Idő</th><th>Üzenet</th></tr></thead>"
                      f"<tbody>{rows}</tbody></table>" if rows else f"<div class='error'>{e(last.get('error'))}</div>"))
    its = t.get("iterations") or []
    if its:
        out.append("<h3>Javító iterációk</h3>")
        for it in its:
            findings = "".join(f"<li>{badge('LLM ' + f['by'], f['by'].lower())} <span class=mono>{e(f['test'])}</span>: "
                               f"{e(f['root_cause'])} → <em>{e(f['fix_target'])}</em></li>" for f in it.get("findings", []))
            out.append(f'<div class="panel"><h4>{it["iteration"]}. iteráció {status(it["status"])}</h4>'
                       f'<p class="muted">előtte {len(it["failing_before"])} sikertelen → utána {len(it["failing_after"])}'
                       f'{" · módosítva: " + e(", ".join(it["changed_files"])) if it.get("changed_files") else ""}</p>'
                       f'{"<ul>" + findings + "</ul>" if findings else ""}</div>')
    return "".join(out)


def sec_consensus(ctx: Ctx) -> str:
    recs = ctx.p.get("consensus") or []
    if not recs:
        return '<p class="muted">Nincs közös döntés.</p>'
    names = {"pipeline_analysis": "Teljes folyamat – elemzések", "design_architecture": "Programtervezés – architektúra"}
    out = []
    for rec in reversed(recs):
        ag = rec.get("aggregate") or {}
        src = rec["source"]
        src = f"Aréna {src.split(':')[1]}. kör" if src.startswith("arena:") else names.get(src, src)
        table = ""
        if ag:
            def leader(c):
                v = ag["leaders"].get(c)
                return "≈ egyenlő" if v == "tie" else badge("LLM " + v, v.lower()) if v else "–"
            table = ("<table><thead><tr><th>Szempont</th><th>LLM A megoldása</th><th>LLM B megoldása</th><th>Erősebb</th></tr></thead><tbody>"
                     + "".join(f"<tr><td>{CRITERIA_HU[c]}</td><td>{score_bar('A', ag['table'][c]['A'])}</td>"
                               f"<td>{score_bar('B', ag['table'][c]['B'])}</td><td>{leader(c)}</td></tr>" for c in CRITERIA)
                     + f"<tr class=total><td>Átlag</td><td>{score_bar('A', ag['averages']['A'])}</td>"
                       f"<td>{score_bar('B', ag['averages']['B'])}</td><td></td></tr></tbody></table>")
        ideas = "".join(f"<li>{badge('LLM ' + i['from'], i['from'].lower()) if i['from'] in ('A', 'B') else badge('?')} "
                        f"{e(i['idea'])}</li>" for i in ag.get("best_ideas", []))
        rv = rec.get("review") or {}
        out.append(f'<section class="panel"><h3>{e(src)} {status(rec.get("status"))} '
                   f'<span class="muted">{fmt_ts(rec.get("created"))} · szintetizáló: LLM {e(rec.get("synthesizer"))}</span></h3>'
                   f'{table}<div class="grid2">'
                   f'<div><h4>Erősségek – A</h4>{ul((ag.get("strengths") or {}).get("A"))}<h4>Gyengeségek – A</h4>{ul((ag.get("weaknesses") or {}).get("A"))}</div>'
                   f'<div><h4>Erősségek – B</h4>{ul((ag.get("strengths") or {}).get("B"))}<h4>Gyengeségek – B</h4>{ul((ag.get("weaknesses") or {}).get("B"))}</div></div>'
                   f'<h4>Átvett legjobb ötletek</h4>{"<ul>" + ideas + "</ul>" if ideas else "<p class=muted>–</p>"}'
                   + (f'<h4>Feloldandó ellentmondások</h4>{ul(ag.get("conflicts"))}' if ag.get("conflicts") else "")
                   + (f'<h4>🤝 Közösen összeállított megoldás</h4><div class="conclusion">{markdown(rec["merged"])}</div>'
                      if rec.get("merged") else "")
                   + (f'<p class="muted">Kereszt-ellenőrzés (LLM {e(rv.get("by"))}): '
                      f'{"✅ jóváhagyva" if rv.get("approved") else "⚠ problémák"}{" → javított változat" if rv.get("revised") else ""}</p>'
                      if rv else "")
                   + "</section>")
    return "".join(out)


def sec_code(ctx: Ctx) -> str:
    code = ctx.p.get("code") or {}
    files = code.get("files") or {}
    if not files:
        return '<p class="muted">Nincs generált kód.</p>'
    versions = code.get("versions") or []
    toc = "".join(f'<li><a href="#file-{i}" class="mono">{e(n)}</a> <span class="muted">({len(files[n].splitlines())} sor)</span></li>'
                  for i, n in enumerate(sorted(files)))
    def numbered(text: str) -> str:
        return "".join("<span>" + (e(line) or " ") + "</span>" for line in text.splitlines())

    listing = "".join(f'<section class="file" id="file-{i}"><h4 class="mono">{e(n)}</h4>'
                      f'<pre class="code lines">{numbered(files[n])}</pre></section>'
                      for i, n in enumerate(sorted(files)))
    hist = "".join(f"<tr><td>v{v['version']}</td><td>{fmt_ts(v['created'])}</td><td>{e(v['source'])}</td>"
                   f"<td>{e(v.get('note'))}</td><td class=mono>{e(', '.join(v.get('changed') or []))}</td></tr>"
                   for v in reversed(versions))
    return (f'<p class="muted">Aktuális verzió: v{len(versions)} · {len(files)} fájl</p><ul class="toc">{toc}</ul>{listing}'
            f'<h3>Verziók</h3><table><thead><tr><th>Verzió</th><th>Idő</th><th>Forrás</th><th>Megjegyzés</th><th>Fájlok</th></tr></thead>'
            f'<tbody>{hist}</tbody></table>')


def sec_pipeline(ctx: Ctx) -> str:
    pl = ctx.p.get("pipeline") or {}
    final = ctx.p.get("final")
    out = []
    if pl.get("task"):
        out.append(f'<div class="panel"><h4>Feladat</h4>{markdown(pl["task"])}</div>')
        out.append("<ol class='steps'>" + "".join(f"<li class='{e(st['status'])}'>{e(st['label'])} {status(st['status'])}</li>"
                                                  for st in pl.get("stages", {}).values()) + "</ol>")
        if pl.get("approach"):
            out.append(f"<h3>Közösen elfogadott megközelítés</h3><div class='conclusion'>{markdown(pl['approach'])}</div>")
    if final:
        tr = final.get("test_report") or {}
        audit = final.get("audit") or {}
        out.append("<h3>🏁 Végleges megoldás</h3>" + stat_row([
            (f"v{final.get('version', '–')}", "Kódverzió"), (len(final.get("files") or []), "Fájl"),
            (tr.get("passed", "–"), "Sikeres teszt"), (tr.get("failed", "–"), "Sikertelen teszt"),
            (len(tr.get("fixed_bugs") or []), "Javított hiba"), ("✔" if audit.get("approved") else "–", "Végső audit")]))
        if audit.get("scores"):
            out.append("<p>" + " ".join(badge(f"{CRITERIA_HU[c]}: {audit['scores'].get(c, '–')}") for c in CRITERIA) + "</p>")
        if audit.get("remaining_issues"):
            out.append(f"<h4>Fennmaradó problémák (audit)</h4>{ul(audit['remaining_issues'])}")
        if final.get("report"):
            out.append(f"<h3>Jelentés</h3><div class='conclusion'>{markdown(final['report'])}</div>")
    return "".join(out) or '<p class="muted">Nem futott teljes folyamat.</p>'


RENDERERS: dict[str, Callable[[Ctx], str]] = {
    "pipeline": sec_pipeline, "arena": sec_arena, "debate": sec_debate, "consensus": sec_consensus,
    "design": sec_design, "testing": sec_testing, "code": sec_code,
}

HAS_CONTENT: dict[str, Callable[[dict], bool]] = {
    "pipeline": lambda p: bool((p.get("pipeline") or {}).get("task") or p.get("final")),
    "arena": lambda p: bool((p.get("arena") or {}).get("rounds")),
    "debate": lambda p: bool((p.get("debate") or {}).get("topic")),
    "consensus": lambda p: bool(p.get("consensus")),
    "design": lambda p: bool((p.get("design") or {}).get("requirements")),
    "testing": lambda p: bool((p.get("testing") or {}).get("runs") or (p.get("testing") or {}).get("report")),
    "code": lambda p: bool((p.get("code") or {}).get("files")),
}


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

CSS = """
:root{--bg:#f5f7fb;--card:#fff;--text:#1a2030;--muted:#687288;--border:#dfe4ee;--soft:#f0f3f9;--accent:#6c5ce7;
--a:#3d7bf2;--a-soft:#eaf1ff;--b:#f07a2c;--b-soft:#fff1e7;--ok:#1f9d63;--err:#d93847;--warn:#c98a0b;--code:#f6f8fc}
@media (prefers-color-scheme:dark){:root{--bg:#0f131b;--card:#171d29;--text:#e6e9f0;--muted:#8d97ad;--border:#2a3247;
--soft:#1e2535;--a-soft:#16233d;--b-soft:#34200f;--code:#0b0e14;--ok:#3ccf8e;--err:#ff6b78;--warn:#f5b942}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 64px}
.hero{background:linear-gradient(135deg,var(--a),var(--accent) 55%,var(--b));color:#fff;border-radius:18px;padding:28px 30px;margin-bottom:26px;box-shadow:0 10px 30px rgba(40,40,90,.18)}
.hero h1{margin:0 0 6px;font-size:28px;letter-spacing:.2px}.hero p{margin:2px 0;opacity:.92}
.hero .models{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.hero .models span{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.35);padding:4px 12px;border-radius:99px;font-size:13px}
nav.toc-top{display:flex;gap:8px;flex-wrap:wrap;margin:-8px 0 22px}
nav.toc-top a{background:var(--card);border:1px solid var(--border);border-radius:99px;padding:4px 12px;color:var(--text);text-decoration:none;font-size:13px}
h2.section{font-size:22px;margin:40px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--border)}
h3{font-size:18px;margin:26px 0 10px}h4{font-size:15.5px;margin:18px 0 6px}h5,h6{font-size:14.5px;margin:14px 0 6px}
.muted{color:var(--muted);font-weight:400;font-size:.9em}
.panel{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px 20px;margin:12px 0}
.panel>:first-child,.conclusion>:first-child,.prompt>:first-child,.msg .body>:first-child{margin-top:0}
.panel>:last-child,.conclusion>:last-child,.prompt>:last-child,.msg .body>:last-child{margin-bottom:0}
.panel.highlight{border-top:4px solid var(--accent)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:12px 0}
.stat{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 16px}
.stat b{display:block;font-size:26px;line-height:1.2}.stat span{color:var(--muted);font-size:13px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.versus{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}
@media (max-width:820px){.grid2,.versus{grid-template-columns:1fr}}
.prompt{background:var(--soft);border:1px solid var(--border);border-radius:12px;padding:10px 16px;margin-bottom:12px}
.msg{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden;margin:8px 0}
.msg header{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:9px 16px;border-bottom:1px solid var(--border)}
.msg.slot-A{border-left:5px solid var(--a)}.msg.slot-A header{background:var(--a-soft)}
.msg.slot-B{border-left:5px solid var(--b)}.msg.slot-B header{background:var(--b-soft)}
.msg .body{padding:12px 18px}.msg footer{padding:7px 16px;border-top:1px solid var(--border);color:var(--muted);font-size:12.5px}
.model{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--muted)}.grow{flex:1}
.timeline{display:flex;flex-direction:column;gap:6px}.turn{max-width:88%}.turn.slot-B{align-self:flex-end}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 10px}
.chip{font-size:12px;border:1px solid var(--border);background:var(--card);border-radius:7px;padding:2px 8px}
.chip.claims{border-color:var(--ok)}.chip.objections{border-color:var(--err)}.chip.concessions{border-color:var(--accent)}.chip.questions{border-color:var(--warn)}
.badge{display:inline-block;font-size:11.5px;font-weight:600;padding:1px 9px;border-radius:99px;border:1px solid var(--border);color:var(--muted);vertical-align:middle;white-space:nowrap}
.badge.a{color:var(--a);border-color:var(--a);background:var(--a-soft)}.badge.b{color:var(--b);border-color:var(--b);background:var(--b-soft)}
.badge.done,.badge.passed{color:var(--ok);border-color:var(--ok)}.badge.error,.badge.failed,.badge.timeout,.badge.critical,.badge.high{color:var(--err);border-color:var(--err)}
.badge.medium,.badge.running{color:var(--warn);border-color:var(--warn)}
table{width:100%;border-collapse:collapse;margin:10px 0;font-size:14px;background:var(--card);border-radius:10px;overflow:hidden}
th{text-align:left;font-size:12.5px;color:var(--muted);background:var(--soft);padding:8px 10px}td{padding:8px 10px;border-top:1px solid var(--border);vertical-align:top}
tr.total td{font-weight:700}tr.tb td{background:var(--code)}
.score{display:flex;align-items:center;gap:8px}.score span{width:32px;font-variant-numeric:tabular-nums}
.score .bar{flex:1;height:8px;background:var(--soft);border-radius:5px;overflow:hidden;min-width:70px}.score .bar i{display:block;height:100%}
.score.a i{background:var(--a)}.score.b i{background:var(--b)}
pre{white-space:pre-wrap;word-break:break-word}
pre.code{position:relative;background:var(--code);border:1px solid var(--border);border-radius:10px;padding:14px 16px;overflow:auto;font:13px/1.55 ui-monospace,Menlo,Consolas,monospace;white-space:pre}
pre.code .lang{position:absolute;top:4px;right:10px;font-size:10.5px;color:var(--muted);text-transform:uppercase}
pre.code.lines{counter-reset:ln;padding-left:0}pre.code.lines span{display:block}
pre.code.lines span::before{counter-increment:ln;content:counter(ln);display:inline-block;width:42px;margin-right:14px;padding-right:10px;text-align:right;color:var(--muted);border-right:1px solid var(--border)}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.9em;background:var(--soft);padding:1px 5px;border-radius:5px}pre code{background:none;padding:0}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
blockquote{border-left:4px solid var(--border);margin:8px 0;padding:2px 14px;color:var(--muted)}
li.sub{margin-left:18px}.conclusion{background:var(--card);border:1px solid var(--border);border-left:4px solid var(--accent);border-radius:12px;padding:12px 18px}
.error{background:rgba(217,56,71,.08);border:1px solid var(--err);border-radius:10px;padding:8px 12px;margin:8px 0}
details.tools{background:var(--soft);border:1px solid var(--border);border-radius:10px;padding:8px 12px;margin-bottom:10px;font-size:13px}
details.tools summary{cursor:pointer;color:var(--muted)}.tool{margin-top:6px}.tool ol{margin:4px 0 0;padding-left:22px}
details.reasoning{margin-bottom:10px;color:var(--muted);font-size:13px}details.reasoning pre{max-height:320px;overflow:auto}
ol.steps li{margin:4px 0}ul.toc{columns:2}a{color:var(--accent)}
.stage{border-left:3px solid var(--border);padding-left:16px;margin:18px 0}
footer.doc{margin-top:48px;color:var(--muted);font-size:12.5px;text-align:center}
@media print{body{background:#fff;color:#000;font-size:12px}.wrap{max-width:none;padding:0}.hero{box-shadow:none;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.msg,.panel,section,table,pre{break-inside:avoid}nav.toc-top{display:none}details.reasoning{display:none}}
"""


def render(project: dict, section: str = "all") -> str:
    if section not in SECTIONS:
        raise ValueError(f"Ismeretlen szakasz: {section}")
    ctx = Ctx(project)
    keys = list(RENDERERS) if section == "all" else [section]
    if section == "all":  # skip empty sections in the full report
        keys = [k for k in keys if HAS_CONTENT[k](project)] or keys
    heading = (lambda k: f'<h2 class="section" id="s-{k}">{SECTIONS[k]}</h2>') if len(keys) > 1 else (lambda k: "")
    body = "".join(heading(k) + RENDERERS[k](ctx) for k in keys)
    toc = ('<nav class="toc-top">' + "".join(f'<a href="#s-{k}">{SECTIONS[k]}</a>' for k in keys) + "</nav>"
           if len(keys) > 1 else "")
    llms = project.get("llms", {})
    models = "".join(f"<span>LLM {s}: {e(llms.get(s, {}).get('name'))} · {e(llms.get(s, {}).get('model') or 'auto')}</span>"
                     for s in ("A", "B"))
    title = f"{SECTIONS[section]} – {project.get('name', '')}"
    task = project.get("task") or ""
    return (f'<!doctype html><html lang="hu"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{e(title)}</title><style>{CSS}</style></head><body><div class="wrap">'
            f'<header class="hero"><h1>{e(SECTIONS[section])}</h1><p><strong>{e(project.get("name"))}</strong></p>'
            + (f"<p>{e(task[:300])}{'…' if len(task) > 300 else ''}</p>" if task else "")
            + f'<p>Exportálva: {fmt_ts(time.time())}</p><div class="models">{models}</div></header>'
            f'{toc}{body}<footer class="doc">LLM Aréna · {e(project.get("id"))}</footer></div></body></html>')
