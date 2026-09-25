// Automatic testing: generate → run → analyse → fix loop, with results.
import { api } from "../api.js";
import { state, trackJob, runningJobs, refresh } from "../state.js";
import { esc, toast, runningBanner, statusBadge, fmtDate, messageCard, lastFinishedError, slotBadge } from "../ui.js";

let root;
let showTb = new Set();
const CAT = { unit: "Unit", integration: "Integrációs", edge_case: "Edge-case", error_handling: "Hibakezelés", performance: "Teljesítmény" };

function testsTable(run) {
  if (!run) return '<div class="empty">Még nem futottak tesztek.</div>';
  if (!run.tests.length) return `<div class="errbox">${esc(run.error || "Nincs teszteredmény.")}</div>${run.stderr ? `<pre class="tb">${esc(run.stderr)}</pre>` : ""}`;
  return `<table class="tbl"><thead><tr><th>Állapot</th><th>Kategória</th><th>Teszt</th><th>Fájl</th><th>Idő</th><th>Üzenet</th></tr></thead><tbody>
    ${run.tests.map((t) => `<tr class="clickable" data-tb="${esc(t.id)}"><td>${statusBadge(t.status)}</td><td>${esc(CAT[t.category] || t.category)}</td>
      <td class="mono">${esc(t.id)}</td><td class="mono">${esc(t.file)}</td><td>${t.duration != null ? (t.duration * 1000).toFixed(0) + " ms" : "–"}</td>
      <td>${esc(t.message)}</td></tr>
      ${showTb.has(t.id) && t.traceback ? `<tr><td colspan="6"><pre class="tb">${esc(t.traceback)}</pre></td></tr>` : ""}`).join("")}
  </tbody></table>`;
}

function iterationsHtml(its) {
  if (!its.length) return '<div class="hint">Még nem volt javító iteráció.</div>';
  return [...its].reverse().slice(0, 20).map((it) => `<div class="card">
    <div class="card-head"><h3>${it.iteration}. iteráció</h3>${statusBadge(it.status)}
      <span class="hint">${fmtDate(it.created)} · előtte ${it.failing_before.length} sikertelen → utána ${it.failing_after.length}</span>
      ${it.code_version ? `<span class="badge">kód v${it.code_version}</span>` : ""}</div>
    ${it.findings.length ? `<table class="tbl"><thead><tr><th>Elemző</th><th>Teszt</th><th>Gyökérok</th><th>Javítandó</th><th>Javaslat</th></tr></thead><tbody>
      ${it.findings.map((f) => `<tr><td>${slotBadge(f.by)}</td><td class="mono">${esc(f.test)}</td><td>${esc(f.root_cause)}</td><td>${esc(f.fix_target)}</td><td>${esc(f.suggestion)}</td></tr>`).join("")}</tbody></table>` : ""}
    ${it.changed_files.length ? `<div class="hint mt">Módosított fájlok: <span class="mono">${esc(it.changed_files.join(", "))}</span></div>` : ""}
    ${it.fixed.length ? `<div class="mt">✅ Javítva: <span class="mono">${esc(it.fixed.join(", "))}</span></div>` : ""}
    <details class="plain mt"><summary>Modellválaszok (${it.messages.length})</summary><div class="stepper mt">${it.messages.map((id) => messageCard(id)).join("")}</div></details>
  </div>`).join("");
}

async function start(action) {
  try {
    const body = action === "loop" ? { max_iterations: +root.querySelector("#t-iter").value } : {};
    trackJob((await api.testing(action, body)).job);
  } catch (e) { toast(e.message, "error"); }
}

export default {
  mount(el) {
    root = el;
    root.innerHTML = `<h1>Automatikus tesztelés</h1>
      <p class="subtitle">A fejlesztő modell unit + integrációs, a reviewer edge-case, hibakezelési és teljesítménytesztet ír. Bukás esetén mindkét modell elemzi a hibát, a fejlesztő javít, a tesztek újrafutnak – a megadott maximális iterációig.</p>
      <div id="t-exec"></div>
      <div class="card"><div class="row">
        <label class="field" style="max-width:180px"><span>Max. javító iteráció</span><input type="number" id="t-iter" min="0" max="10" value="${state.project.settings.max_fix_iterations}"></label>
        <span class="spacer"></span>
        <button class="btn" data-t="generate">🧪 Tesztek generálása</button>
        <button class="btn" data-t="run">▶ Tesztek futtatása</button>
        <button class="btn primary" data-t="loop">⟳ Teszt → javítás ciklus</button>
      </div></div>
      <div id="t-banner"></div><div id="t-body"></div>`;
    root.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-t]");
      if (b) { start(b.dataset.t); return; }
      if (e.target.id === "t-enable") {
        if (!confirm("Engedélyezed a generált kód futtatását ezen a gépen?")) return;
        await api.saveConfig({ settings: { allow_code_execution: true } });
        state.project.settings.allow_code_execution = true;
        refresh(10);
        this.update("project");
        return;
      }
      const tr = e.target.closest("[data-tb]");
      if (tr) { const id = tr.dataset.tb; if (showTb.has(id)) showTb.delete(id); else showTb.add(id); this.update("project"); }
    });
  },
  update(reason) {
    root.querySelector("#t-banner").innerHTML = runningBanner(["testing", "design", "pipeline"]);
    const busy = runningJobs("testing").length || runningJobs("design").length || runningJobs("pipeline").length;
    const t = state.project.testing;
    const hasCode = Object.keys(state.project.code.files).length > 0;
    const allow = state.project.settings.allow_code_execution;
    root.querySelectorAll("[data-t]").forEach((b) => { b.disabled = !!busy || !hasCode || (b.dataset.t !== "generate" && !allow); });
    root.querySelector("#t-exec").innerHTML = allow ? "" : `<div class="warnbox">A generált kód futtatása ki van kapcsolva. A tesztek generálhatók, de nem futtathatók.
      <button class="btn small" id="t-enable">Engedélyezés</button></div>`;
    if (reason === "jobs") return;
    const rep = t.report;
    const last = t.runs[t.runs.length - 1];
    const cats = rep?.by_category || last?.summary?.by_category || {};
    root.querySelector("#t-body").innerHTML = `
      ${lastFinishedError("testing")}
      ${rep ? `<div class="stats">
        <div class="stat ok"><div class="v">${rep.passed}</div><div class="k">Sikeres teszt</div></div>
        <div class="stat ${rep.failed ? "err" : ""}"><div class="v">${rep.failed}</div><div class="k">Sikertelen teszt</div></div>
        <div class="stat"><div class="v">${rep.fixed_bugs.length}</div><div class="k">Javított hiba</div></div>
        <div class="stat ${rep.remaining_issues.length ? "warn" : ""}"><div class="v">${rep.remaining_issues.length}</div><div class="k">Fennmaradó probléma</div></div>
        <div class="stat"><div class="v">${rep.iterations}/${rep.max_iterations}</div><div class="k">Javító iteráció</div></div>
        <div class="stat"><div class="v">v${rep.final_version}</div><div class="k">Végleges kód <a href="#code" data-tab="code" data-goto>→</a></div></div>
      </div>` : hasCode ? "" : '<div class="empty">Még nincs kód. Előbb futtass programtervezést (vagy hozz létre fájlt a Kódnézetben).</div>'}
      ${Object.keys(cats).length ? `<div class="row" style="margin-bottom:12px">${Object.entries(cats).map(([k, v]) => `<span class="badge ${v.passed === v.total ? "ok" : "err"}">${esc(CAT[k] || k)}: ${v.passed}/${v.total}</span>`).join("")}</div>` : ""}
      ${rep && rep.remaining_issues.length ? `<div class="card"><h3>Fennmaradó problémák</h3><ul class="clean">${rep.remaining_issues.map((r) => `<li class="mono">${esc(r)}</li>`).join("")}</ul></div>` : ""}
      ${rep && rep.fixed_bugs.length ? `<div class="card"><h3>Javított hibák</h3><table class="tbl"><thead><tr><th>Teszt</th><th>Iteráció</th><th>Gyökérok</th><th>Javítás helye</th><th>Kódverzió</th></tr></thead><tbody>
        ${rep.fixed_bugs.map((b) => `<tr><td class="mono">${esc(b.test)}</td><td>${b.iteration}</td><td>${esc(b.root_cause)}</td><td>${esc(b.fix_target)}</td><td>v${b.code_version}</td></tr>`).join("")}</tbody></table></div>` : ""}
      <h2>Legutóbbi futtatás ${last ? `<span class="hint">· ${fmtDate(last.created)} · kód v${last.code_version} · ${last.duration}s${last.timed_out ? " · IDŐTÚLLÉPÉS" : ""}</span>` : ""}</h2>
      ${testsTable(last)}
      ${last && (last.stdout || last.stderr) ? `<details class="plain mt"><summary>Kimenet (stdout/stderr)</summary><pre class="tb">${esc(last.stdout)}\n${esc(last.stderr)}</pre></details>` : ""}
      <h2>Javító iterációk</h2>${iterationsHtml(t.iterations)}
      <h2>Futtatási előzmények</h2>
      <table class="tbl"><thead><tr><th>Idő</th><th>Iteráció</th><th>Kód</th><th>Sikeres</th><th>Sikertelen</th><th>Időtartam</th></tr></thead><tbody>
        ${[...t.runs].reverse().slice(0, 30).map((r) => `<tr><td>${fmtDate(r.created)}</td><td>${r.iteration}</td><td>v${r.code_version}</td><td>${r.summary.passed ?? 0}/${r.summary.total ?? 0}</td><td>${r.summary.failing ?? 0}</td><td>${r.duration}s</td></tr>`).join("") || '<tr><td colspan="6" class="hint">nincs</td></tr>'}
      </tbody></table>`;
  },
};
