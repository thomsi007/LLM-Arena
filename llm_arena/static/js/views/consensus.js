// Joint decision: criterion scores, best ideas, combined solution.
import { api } from "../api.js";
import { state, trackJob, runningJobs } from "../state.js";
import { esc, toast, runningBanner, markdown, list, fmtDate, statusBadge, messageCard, CRITERIA, CRITERIA_HU, slotBadge, copyText, lastFinishedError, exportHtmlBtn } from "../ui.js";

let root;
const SRC = { pipeline_analysis: "Teljes folyamat – elemzések", design_architecture: "Programtervezés – architektúra" };

function bar(slot, v) {
  return `<div class="scorebar ${slot.toLowerCase()}"><span class="mono" style="width:30px">${v ?? "–"}</span><div class="bar"><div style="width:${(v || 0) * 10}%"></div></div></div>`;
}

function recHtml(rec) {
  const ag = rec.aggregate;
  const table = ag ? `<table class="tbl"><thead><tr><th>Szempont</th><th>LLM A megoldása</th><th>LLM B megoldása</th><th>Erősebb</th></tr></thead><tbody>
    ${CRITERIA.map((c) => `<tr><td>${CRITERIA_HU[c]}</td><td>${bar("A", ag.table[c].A)}</td><td>${bar("B", ag.table[c].B)}</td>
      <td class="leader">${ag.leaders[c] === "tie" ? "≈ egyenlő" : ag.leaders[c] ? slotBadge(ag.leaders[c]) : "–"}</td></tr>`).join("")}
    <tr><td><b>Átlag</b></td><td>${bar("A", ag.averages.A)}</td><td>${bar("B", ag.averages.B)}</td><td></td></tr></tbody></table>` : "";
  const ideas = ag?.best_ideas?.length
    ? `<ul class="clean">${ag.best_ideas.map((i) => `<li>${i.from === "A" || i.from === "B" ? slotBadge(i.from) : '<span class="badge">?</span>'} ${esc(i.idea)} <span class="hint">(értékelte: ${esc(i.evaluator)})</span></li>`).join("")}</ul>`
    : '<div class="hint">–</div>';
  const src = rec.source.startsWith("arena:") ? `Aréna ${rec.source.split(":")[1]}. kör` : SRC[rec.source] || rec.source;
  const rv = rec.review;
  return `<div class="card">
    <div class="card-head"><h2>${esc(src)}</h2>${statusBadge(rec.status)}<span class="hint">${fmtDate(rec.created)} · szintetizáló: LLM ${esc(rec.synthesizer)}</span></div>
    <p class="hint">Nem győztes–vesztes: mindkét modell mindkét (anonimizált) megoldást pontozza, a szempontonkénti átlagok mutatják, melyik megoldás miben erősebb; a végeredmény a kettő legjobb ötleteiből épül.</p>
    ${table}
    <div class="grid2 mt">
      <div><h3>Erősségek – A</h3>${list(ag?.strengths?.A)}<h3>Gyengeségek – A</h3>${list(ag?.weaknesses?.A)}</div>
      <div><h3>Erősségek – B</h3>${list(ag?.strengths?.B)}<h3>Gyengeségek – B</h3>${list(ag?.weaknesses?.B)}</div>
    </div>
    <h3>Átvett legjobb ötletek</h3>${ideas}
    ${ag?.conflicts?.length ? `<h3>Feloldandó ellentmondások</h3>${list(ag.conflicts)}` : ""}
    ${rec.merged ? `<div class="row"><h3>🤝 Közösen összeállított megoldás</h3><span class="spacer"></span><button class="btn small" data-copy-merged="${rec.id}">Másolás</button></div>
      <div class="card" style="box-shadow:none"><div class="md">${markdown(rec.merged)}</div></div>` : ""}
    ${rv ? `<div class="hint">Kereszt-ellenőrzés (LLM ${esc(rv.by)}): ${rv.approved ? "✅ jóváhagyva" : "⚠ problémák"}${rv.revised ? " → javított változat" : ""}</div>${rv.remaining_issues.length ? list(rv.remaining_issues) : ""}` : ""}
    <details class="plain mt"><summary>Modellválaszok (${rec.messages.length})</summary><div class="stepper mt">${rec.messages.map((id) => messageCard(id)).join("")}</div></details>
  </div>`;
}

export default {
  mount(el) {
    root = el;
    root.innerHTML = `<h1>Közös döntés</h1>
      <p class="subtitle">Összehasonlítási szempontok: helyesség, teljesség, műszaki megvalósíthatóság, biztonság, teljesítmény, tesztelhetőség. Indítható az Aréna bármely köréről; a programtervezés és a teljes folyamat automatikusan használja.</p>
      <div class="card"><div class="row"><span class="hint">Döntés az utolsó aréna körről:</span><span class="spacer"></span>
        ${exportHtmlBtn("consensus")}
        <button class="btn primary" id="cons-last">⚖ Közös döntés indítása</button></div></div>
      <div id="cons-banner"></div><div id="cons-body"></div>`;
    root.querySelector("#cons-last").onclick = async () => {
      try { trackJob((await api.consensus({})).job); } catch (e) { toast(e.message, "error"); }
    };
    root.addEventListener("click", (e) => {
      const b = e.target.closest("[data-copy-merged]");
      if (b) copyText(state.project.consensus.find((c) => c.id === b.dataset.copyMerged)?.merged || "");
    });
  },
  update(reason) {
    root.querySelector("#cons-banner").innerHTML = runningBanner(["consensus"]);
    root.querySelector("#cons-last").disabled = runningJobs("consensus").length > 0 || !state.project.arena.rounds.length;
    if (reason === "jobs") return;
    const recs = [...state.project.consensus].reverse();
    root.querySelector("#cons-body").innerHTML = lastFinishedError("consensus") +
      (recs.length ? recs.map(recHtml).join("") : '<div class="empty">Még nincs közös döntés.</div>');
  },
};
