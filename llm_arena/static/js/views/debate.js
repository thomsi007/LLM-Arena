// Structured debate: A proponent vs B critic, ledger + synthesis.
import { api } from "../api.js";
import { state, trackJob, runningJobs } from "../state.js";
import { esc, toast, messageCard, runningBanner, list, statusBadge, lastFinishedError, markdown } from "../ui.js";

let root;
const PHASE = { position: "Álláspont", critique: "Kritika", rebuttal: "Válasz a kritikára", counter: "Válasz az új érvekre" };
const ROLE = { proponent: "Érvelő / javaslattevő", critic: "Kritikus / ellenérvelő" };
const CHIP = { claims: "érv", objections: "kifogás", concessions: "elfogadott", questions: "kérdés", proposals: "javaslat" };

function chips(structured) {
  if (!structured) return "";
  const out = [];
  for (const [k, label] of Object.entries(CHIP)) {
    for (const t of structured[k] || []) out.push(`<span class="chip ${k}" title="${label}">${label}: ${esc(t)}</span>`);
  }
  return out.length ? `<div class="chips">${out.join("")}</div>` : "";
}

function synthesisHtml(d) {
  const s = d.synthesis;
  if (!s) return "";
  const disputed = (s.disputed || []).length
    ? `<table class="tbl"><thead><tr><th>Vitatott pont</th><th>LLM A (érvelő)</th><th>LLM B (kritikus)</th></tr></thead><tbody>${s.disputed
      .map((x) => `<tr><td>${esc(x.point)}</td><td>${esc(x.proponent)}</td><td>${esc(x.critic)}</td></tr>`).join("")}</tbody></table>`
    : '<div class="hint">Nincs vitatott pont.</div>';
  const rv = d.synthesis_review;
  return `<div class="card"><div class="card-head"><h2>Vita eredménye</h2>
      <span class="badge">moderátor: LLM ${esc(d.moderator)}</span>${rv ? `<span class="badge">ellenőrizte: LLM ${esc(rv.by)} ${rv.agree ? "✔" : "✎"}</span>` : ""}
      ${s.parsed === false ? '<span class="badge medium">nem strukturált összegzés</span>' : ""}<span class="spacer"></span>
      <button class="btn small" id="copy-synth">Másolás</button></div>
    <div class="grid2">
      <div><h3>Érvek (A)</h3>${list(s.arguments_summary)}</div>
      <div><h3>Ellenérvek (B)</h3>${list(s.counterarguments_summary)}</div>
    </div>
    <h3>✅ Közös ténylista</h3>${list(s.facts)}
    <h3>⚡ Vitatott pontok</h3>${disputed}
    <div class="grid2">
      <div><h3>💡 Megoldási lehetőségek</h3>${list(s.solutions)}</div>
      <div><h3>❓ Nyitott kérdések</h3>${list(s.open_questions)}</div>
    </div>
    <h3>🤝 Közös következtetés</h3><div class="md">${markdown(s.conclusion || "–")}</div>
    ${s.conclusion_comment ? `<p class="hint">LLM ${esc(rv?.by)} megjegyzése: ${esc(s.conclusion_comment)}</p>` : ""}
    ${(s.corrections || []).length ? `<h3>Javítások az ellenőrzésből</h3>${list(s.corrections)}` : ""}
  </div>`;
}

function ledgerHtml(d) {
  const L = d.ledger || {};
  const count = (k, by) => (L[k] || []).filter((i) => !by || i.by === by).length;
  return `<div class="stats">
    <div class="stat"><div class="v">${count("claims", "A")}</div><div class="k">A érvei</div></div>
    <div class="stat"><div class="v">${count("objections", "B")}</div><div class="k">B kifogásai</div></div>
    <div class="stat"><div class="v">${count("concessions")}</div><div class="k">Elfogadott pontok</div></div>
    <div class="stat"><div class="v">${count("questions")}</div><div class="k">Nyitott kérdések</div></div>
    <div class="stat"><div class="v">${count("proposals")}</div><div class="k">Javaslatok</div></div>
  </div>`;
}

function synthText(s) {
  const l = (a) => (a || []).map((x) => "- " + x).join("\n");
  return `Közös tények:\n${l(s.facts)}\n\nVitatott pontok:\n${(s.disputed || []).map((d) => `- ${d.point} (A: ${d.proponent} | B: ${d.critic})`).join("\n")}\n\nMegoldások:\n${l(s.solutions)}\n\nNyitott kérdések:\n${l(s.open_questions)}\n\nKözös következtetés:\n${s.conclusion}`;
}

export default {
  mount(el) {
    root = el;
    const s = state.project.settings;
    root.innerHTML = `<h1>Vita</h1>
      <p class="subtitle"><span class="badge a">LLM A</span> érvelő / javaslattevő · <span class="badge b">LLM B</span> kritikus / ellenérvelő.
      Menet: A álláspont → B kritika → A válasz → B válasz (… körönként) → összegzés és kereszt-ellenőrzés.</p>
      <div class="card">
        <label class="field"><span>Vitatéma / feladat</span><textarea id="deb-topic" rows="3" placeholder="pl. Monolit vagy mikroszolgáltatás egy 5 fős csapat új SaaS termékéhez?">${esc(state.project.debate?.topic || "")}</textarea></label>
        <div class="row">
          <label class="field" style="max-width:140px"><span>Körök száma</span><input type="number" id="deb-rounds" min="1" max="8" value="${s.debate_rounds}"></label>
          <span class="spacer"></span>
          <button class="btn" id="deb-resume">↻ Folytatás / újrapróbálás</button>
          <button class="btn primary" id="deb-start">▶ Új vita indítása</button>
        </div>
      </div>
      <div id="deb-banner"></div><div id="deb-body"></div>`;
    root.querySelector("#deb-start").onclick = async () => {
      const topic = root.querySelector("#deb-topic").value.trim();
      if (!topic) { toast("Adj meg vitatémát.", "warn"); return; }
      try { trackJob((await api.debate({ topic, rounds: +root.querySelector("#deb-rounds").value })).job); } catch (e) { toast(e.message, "error"); }
    };
    root.querySelector("#deb-resume").onclick = async () => {
      try { trackJob((await api.debate({ resume: true })).job); } catch (e) { toast(e.message, "error"); }
    };
    root.addEventListener("click", (e) => {
      if (e.target.id === "copy-synth") navigator.clipboard.writeText(synthText(state.project.debate.synthesis)).then(() => toast("Másolva", "ok", 1200));
    });
  },
  update(reason) {
    root.querySelector("#deb-banner").innerHTML = runningBanner(["debate", "pipeline"]);
    const busy = runningJobs("debate").length || runningJobs("pipeline").length;
    root.querySelector("#deb-start").disabled = !!busy;
    const d = state.project.debate;
    root.querySelector("#deb-resume").disabled = !!busy || !d?.topic || d.status === "done";
    if (reason === "jobs") return;
    if (!d || !d.topic) { root.querySelector("#deb-body").innerHTML = '<div class="empty">Még nincs vita.</div>'; return; }
    const turns = d.turns.map((t) => `<div class="turn slot-${t.slot}">
        ${messageCard(t.msg_id, { content: t.status === "done" ? t.content : undefined, slot: t.slot, title: `${t.round}. kör · ${PHASE[t.phase]} · ${ROLE[t.role]}`, placeholder: "…" })}
        ${chips(t.structured)}</div>`).join("");
    root.querySelector("#deb-body").innerHTML = `
      <div class="row"><h2>Vita menete</h2>${statusBadge(d.status)}<span class="hint">${d.turns.filter((t) => t.status === "done").length} / ${d.rounds * 2} hozzászólás</span></div>
      ${d.error && d.status !== "done" ? `<div class="errbox">${esc(d.error.label || "Hiba")}: ${esc(d.error.message)} – a „Folytatás” gombbal az utolsó sikeres lépéstől folytatható.</div>` : lastFinishedError("debate")}
      <h3>Strukturált állapot</h3>${ledgerHtml(d)}
      <div class="timeline">${turns || '<div class="empty">Indul…</div>'}</div>
      <div class="mt">${synthesisHtml(d)}</div>
      <details class="plain mt"><summary>Belső állapot (JSON)</summary><div class="kv">${esc(JSON.stringify({ ledger: d.ledger, synthesis: d.synthesis }, null, 2))}</div></details>`;
  },
};
