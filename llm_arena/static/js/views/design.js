// Collaborative Coding / Program Designer: 11-stage developer + reviewer workflow.
import { api } from "../api.js";
import { state, trackJob, runningJobs } from "../state.js";
import { esc, toast, messageCard, runningBanner, statusBadge, markdown, jsonBlock, lastFinishedError, exportHtmlBtn } from "../ui.js";

let root;
const open = new Set();
const ICON = { done: "✓", running: "…", error: "!", pending: "", cancelled: "×", skipped: "–" };
const SEV = { critical: 0, high: 1, medium: 2, low: 3 };
const CAT_HU = {
  logic: "logika", security: "biztonság", performance: "teljesítmény", api_misuse: "hibás API-használat",
  edge_cases: "edge case", missing_tests: "hiányzó teszt", maintainability: "karbantarthatóság",
};

function issuesTable(issues) {
  if (!issues?.length) return '<div class="hint">Nincs rögzített probléma.</div>';
  const rows = [...issues].sort((a, b) => (SEV[a.severity] ?? 9) - (SEV[b.severity] ?? 9));
  return `<table class="tbl"><thead><tr><th>ID</th><th>Súlyosság</th><th>Kategória</th><th>Hely</th><th>Leírás</th><th>Javaslat</th><th>Állapot</th></tr></thead><tbody>
    ${rows.map((i) => `<tr><td class="mono">${esc(i.id)}</td><td>${statusBadge(i.severity)}</td><td>${esc(CAT_HU[i.category] || i.category)}</td>
      <td class="mono">${esc(i.location)}</td><td>${esc(i.description)}</td><td>${esc(i.suggestion)}</td><td>${statusBadge(i.status || "open")}</td></tr>`).join("")}
  </tbody></table>`;
}

function stageHtml(d, key) {
  const st = d.stages[key];
  const who = st.actor === "both" ? "mindkettő" : st.actor === "dev" ? `fejlesztő (LLM ${d.developer})` : `reviewer (LLM ${d.reviewer})`;
  const isOpen = open.has(key) || st.status === "running";
  const msgs = (st.messages || []).map((id) => messageCard(id)).join("");
  let extra = "";
  if (st.issues) extra += `<h3>Talált problémák</h3>${issuesTable(st.issues)}`;
  if (st.proposals) extra += `<p class="hint">Két architektúra-javaslat közös döntéssel egyesítve. ${esc(st.note || "")} <a href="#consensus" data-tab="consensus" data-goto>Részletek →</a></p>`;
  if (st.error) extra += `<div class="errbox">${esc(st.error.label || "Hiba")}: ${esc(st.error.message)}</div>`;
  return `<div class="step ${st.status} ${isOpen ? "open" : ""}" data-stage="${key}">
    <div class="step-head" data-toggle="${key}"><span class="ico">${ICON[st.status] ?? ""}</span><strong>${esc(st.label)}</strong>
      <span class="hint">${who}</span><span class="spacer"></span>${statusBadge(st.status)}</div>
    <div class="step-body">
      ${st.text ? `<div class="md">${markdown(st.text)}</div>` : ""}
      ${extra}
      ${st.data && key !== "final" ? `<details class="plain"><summary>Strukturált eredmény (JSON)</summary>${jsonBlock(st.data)}</details>` : ""}
      ${msgs ? `<details class="plain" ${st.status === "running" ? "open" : ""}><summary>Modellválaszok (${st.messages.length})</summary><div class="stepper mt">${msgs}</div></details>` : ""}
    </div></div>`;
}

export default {
  mount(el) {
    root = el;
    const d = state.project.design || {};
    const s = state.project.settings;
    root.innerHTML = `<h1>Közös programtervezés</h1>
      <p class="subtitle">Követelmények → hiányzó követelmények → architektúra → modulok → adatstruktúrák → algoritmusok → kód → kódellenőrzés → tesztek → hibajavítás → végleges verzió.</p>
      <div class="card">
        <label class="field"><span>Követelmények / programleírás</span>
          <textarea id="des-req" rows="5" placeholder="pl. Készíts egy parancssori TODO-kezelőt JSON fájl tárolással, prioritásokkal és határidőkkel.">${esc(d.requirements || state.project.task || "")}</textarea></label>
        <div class="row">
          <label class="field" style="max-width:260px"><span>Elsődleges fejlesztő</span>
            <select id="des-dev"><option value="A" ${s.developer === "A" ? "selected" : ""}>LLM A fejleszt, LLM B review</option>
            <option value="B" ${s.developer === "B" ? "selected" : ""}>LLM B fejleszt, LLM A review</option></select></label>
          <label class="check"><input type="checkbox" id="des-tests" checked> Tesztek futtatása + javító ciklus</label>
          <span class="spacer"></span>
          ${exportHtmlBtn("design")}
          <button class="btn" id="des-resume">↻ Folytatás / újrapróbálás</button>
          <button class="btn primary" id="des-start">▶ Tervezés indítása</button>
        </div>
        ${s.allow_code_execution ? "" : '<div class="warnbox mt">A kódfuttatás ki van kapcsolva – a tesztek generálódnak, de nem futnak. <a href="#settings" data-tab="settings" data-goto>Beállítások →</a></div>'}
      </div>
      <div id="des-banner"></div><div id="des-body"></div>`;
    root.querySelector("#des-start").onclick = async () => {
      const requirements = root.querySelector("#des-req").value.trim();
      if (!requirements) { toast("Adj meg követelményeket.", "warn"); return; }
      if (state.project.design?.requirements && !confirm("Új tervezés indul – a korábbi terv felülíródik (a kódverziók megmaradnak). Folytatod?")) return;
      try {
        trackJob((await api.design({ requirements, developer: root.querySelector("#des-dev").value, run_tests: root.querySelector("#des-tests").checked })).job);
      } catch (e) { toast(e.message, "error"); }
    };
    root.querySelector("#des-resume").onclick = async () => {
      try { trackJob((await api.design({ resume: true, run_tests: root.querySelector("#des-tests").checked })).job); } catch (e) { toast(e.message, "error"); }
    };
    root.addEventListener("click", (e) => {
      const t = e.target.closest("[data-toggle]");
      if (!t) return;
      const k = t.dataset.toggle;
      if (open.has(k)) open.delete(k); else open.add(k);
      t.parentElement.classList.toggle("open");
    });
  },
  update(reason) {
    root.querySelector("#des-banner").innerHTML = runningBanner(["design", "pipeline", "testing", "consensus"]);
    const d = state.project.design;
    const busy = runningJobs("design").length || runningJobs("pipeline").length;
    root.querySelector("#des-start").disabled = !!busy;
    root.querySelector("#des-resume").disabled = !!busy || !d?.requirements || d.status === "done";
    if (reason === "jobs") return;
    if (!d || !d.requirements) { root.querySelector("#des-body").innerHTML = '<div class="empty">Még nincs terv.</div>'; return; }
    const done = d.order.filter((k) => d.stages[k].status === "done").length;
    const final = d.final;
    root.querySelector("#des-body").innerHTML = `
      <div class="row"><h2>Munkafolyamat</h2>${statusBadge(d.status)}<span class="hint">${done} / ${d.order.length} lépés kész ·
        fejlesztő: LLM ${esc(d.developer)} · reviewer: LLM ${esc(d.reviewer)}</span></div>
      ${d.error && d.status !== "done" ? `<div class="errbox">${esc(d.error.label || "Hiba")}: ${esc(d.error.message)} – a „Folytatás” gombbal a hibás lépéstől újrapróbálható.</div>` : lastFinishedError("design")}
      ${final ? `<div class="card"><div class="card-head"><h2>Végleges verzió: v${final.version}</h2><span class="spacer"></span>
        <a class="btn small" href="#code" data-tab="code" data-goto>Kódnézet →</a><a class="btn small" href="/api/code/download">⤓ ZIP</a></div>
        ${final.audit ? `<div class="hint">Végső audit: ${final.audit.approved ? "✅ jóváhagyva" : "⚠ nincs jóváhagyva"}</div>` : ""}
        ${final.test_report ? `<div>Tesztek: <b>${final.test_report.passed}</b> sikeres / <b>${final.test_report.failed}</b> sikertelen, javított hibák: <b>${final.test_report.fixed_bugs.length}</b></div>` : ""}</div>` : ""}
      <div class="stepper">${d.order.map((k) => stageHtml(d, k)).join("")}</div>
      <h2>Review problémák (kódellenőrzés)</h2>${issuesTable(d.review_issues)}`;
  },
};
