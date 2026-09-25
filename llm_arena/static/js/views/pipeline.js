// Full flow: Task → analysis → decision → debate → design → code → test → fix → final.
import { api } from "../api.js";
import { state, trackJob, runningJobs } from "../state.js";
import { esc, toast, runningBanner, markdown, statusBadge, copyText, CRITERIA, CRITERIA_HU, list, lastFinishedError, exportHtmlBtn, showError, errorBox, attachBox, bindAttach, draftIds, setDraft, attachmentList } from "../ui.js";

let root;
const ICON = { done: "✓", running: "…", error: "!", pending: "", cancelled: "×" };
const LINK = { analysis: "arena", consensus: "consensus", debate: "debate", design: "design", report: "pipeline" };

function finalHtml(f) {
  if (!f) return "";
  const tr = f.test_report;
  const audit = f.audit;
  return `<div class="card"><div class="card-head"><h2>🏁 Végleges megoldás</h2><span class="badge">${f.source === "pipeline" ? "teljes folyamat" : "programtervezés"}</span>
      <span class="spacer"></span>${f.report ? '<button class="btn small" id="pl-copy">Másolás</button>' : ""}
      <a class="btn small" href="/api/export/html?section=pipeline">⤓ HTML</a><a class="btn small" href="/api/code/download">⤓ Kód (ZIP)</a><a class="btn small" href="/api/export/conversation?format=md">⤓ Beszélgetés</a></div>
    <div class="stats">
      <div class="stat"><div class="v">v${f.version ?? "–"}</div><div class="k">Kódverzió (${(f.files || []).length} fájl)</div></div>
      ${tr ? `<div class="stat ok"><div class="v">${tr.passed}</div><div class="k">Sikeres teszt</div></div>
      <div class="stat ${tr.failed ? "err" : ""}"><div class="v">${tr.failed}</div><div class="k">Sikertelen teszt</div></div>
      <div class="stat"><div class="v">${tr.fixed_bugs.length}</div><div class="k">Javított hiba</div></div>` : '<div class="stat"><div class="v">–</div><div class="k">Tesztek nem futottak</div></div>'}
      ${audit ? `<div class="stat ${audit.approved ? "ok" : "warn"}"><div class="v">${audit.approved ? "✔" : "⚠"}</div><div class="k">Végső audit</div></div>` : ""}
    </div>
    ${audit?.scores ? `<div class="row">${CRITERIA.map((c) => `<span class="badge">${CRITERIA_HU[c]}: ${esc(audit.scores[c] ?? "–")}</span>`).join("")}</div>` : ""}
    ${audit?.remaining_issues?.length ? `<h3>Fennmaradó problémák (audit)</h3>${list(audit.remaining_issues)}` : ""}
    ${f.report ? `<h3>Jelentés</h3><div class="md">${markdown(f.report)}</div>` : ""}
  </div>`;
}

export default {
  mount(el) {
    root = el;
    const pl = state.project.pipeline || {};
    root.innerHTML = `<h1>Teljes folyamat</h1>
      <p class="subtitle">Feladat → két LLM elemzése → közös döntés → vita → közös tervezés → kód → teszt → javítás → végleges megoldás.</p>
      <div class="card">
        <label class="field"><span>Feladat</span><textarea id="pl-task" rows="5" placeholder="pl. Készíts egy Python modult, amely CSV-ből beolvasott tranzakciókat kategorizál és havi összesítést készít.">${esc(pl.task || state.project.task || "")}</textarea></label>
        ${attachBox("pipeline")}
        <div class="row"><span class="hint">Fejlesztő: LLM ${esc(state.project.settings.developer)} · moderátor: LLM ${esc(state.project.settings.moderator)} ·
          kódfuttatás: ${state.project.settings.allow_code_execution ? "engedélyezve" : "<b>kikapcsolva</b>"} (<a href="#settings" data-tab="settings" data-goto>beállítások</a>)</span>
          <span class="spacer"></span>
          ${exportHtmlBtn("all", "⤓ Teljes riport (HTML)")}
          <button class="btn" id="pl-resume">↻ Folytatás / újrapróbálás</button>
          <button class="btn primary" id="pl-start">▶ Teljes folyamat indítása</button></div>
      </div>
      <div id="pl-banner"></div><div id="pl-body"></div>`;
    if (!state.drafts.pipeline) setDraft("pipeline", state.project.pipeline?.attachments);
    bindAttach(root, "pipeline");
    root.querySelector("#pl-start").onclick = async () => {
      const task = root.querySelector("#pl-task").value.trim();
      const attachments = draftIds("pipeline");
      if (!task && !attachments.length) { toast("Adj meg feladatot, vagy csatolj fájlt.", "warn"); return; }
      if (state.project.pipeline?.task && !confirm("Új folyamat indul – a vita és a terv felülíródik. Folytatod?")) return;
      try { trackJob((await api.pipeline({ task, attachments })).job); } catch (e) { showError(e, "Teljes folyamat"); }
    };
    root.querySelector("#pl-resume").onclick = async () => {
      try { trackJob((await api.pipeline({ resume: true })).job); } catch (e) { showError(e, "Teljes folyamat"); }
    };
    root.addEventListener("click", (e) => { if (e.target.id === "pl-copy") copyText(state.project.final?.report || ""); });
  },
  update(reason) {
    root.querySelector("#pl-banner").innerHTML = runningBanner(["pipeline"]);
    const pl = state.project.pipeline;
    const busy = runningJobs().length > 0;
    root.querySelector("#pl-start").disabled = busy;
    root.querySelector("#pl-resume").disabled = busy || !pl?.task || pl.status === "done";
    if (reason === "jobs") return;
    const steps = pl?.stages ? Object.entries(pl.stages).map(([k, st]) => `<div class="step ${st.status}">
      <div class="step-head"><span class="ico">${ICON[st.status] ?? ""}</span><strong>${esc(st.label)}</strong><span class="spacer"></span>
      ${statusBadge(st.status)}<a class="btn small" href="#${LINK[k]}" data-tab="${LINK[k]}" data-goto>megnyitás →</a></div></div>`).join("") : "";
    root.querySelector("#pl-body").innerHTML = `
      ${pl?.error && pl.status !== "done" ? errorBox({ ...pl.error, hint: (pl.error.hint ? pl.error.hint + " " : "") + "A „Folytatás / újrapróbálás” gombbal az elakadt lépéstől folytatható." }, "A folyamat leállt") : lastFinishedError("pipeline")}
      ${attachmentList(pl?.attachments)}
      ${steps ? `<h2>Lépések ${statusBadge(pl.status)}</h2><div class="stepper">${steps}</div>` : '<div class="empty">Még nem futott teljes folyamat.</div>'}
      ${pl?.approach ? `<details class="plain mt"><summary>Közösen elfogadott megközelítés</summary><div class="card"><div class="md">${markdown(pl.approach)}</div></div></details>` : ""}
      <div class="mt">${finalHtml(state.project.final)}</div>`;
  },
};
