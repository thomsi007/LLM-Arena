// Session / project management: save, load, new, export, import.
import { api } from "../api.js";
import { state, refresh } from "../state.js";
import { esc, toast, fmtDate, exportHtmlBtn } from "../ui.js";
import { probeConnections } from "../app.js";

let root;

async function loadList() {
  try {
    const r = await api.projects();
    root.querySelector("#proj-list").innerHTML = r.projects.length
      ? `<table class="tbl"><thead><tr><th>Név</th><th>Feladat</th><th>Módosítva</th><th>Méret</th><th></th></tr></thead><tbody>
        ${r.projects.map((p) => `<tr><td><b>${esc(p.name)}</b>${p.id === state.project.id ? ' <span class="badge ok">aktuális</span>' : ""}</td>
          <td class="hint">${esc(p.task)}</td><td>${fmtDate(p.updated)}</td><td>${(p.size / 1024).toFixed(1)} kB</td>
          <td style="white-space:nowrap"><button class="btn small" data-load="${p.id}">Betöltés</button>
          <button class="btn small danger" data-del="${p.id}">Törlés</button></td></tr>`).join("")}</tbody></table>`
      : '<div class="empty">Nincs mentett projekt.</div>';
  } catch (e) { toast(e.message, "error"); }
}

async function reloadAll(msg) {
  const data = await api.project();
  state.project = data.project;
  state.live.clear();
  toast(msg, "ok");
  refresh(10);
  probeConnections();
  document.querySelector('#nav a[data-tab="project"]').click();
}

export default {
  mount(el) {
    root = el;
    const p = state.project;
    const counts = {
      "Üzenet": p.messages.length, "Aréna kör": p.arena.rounds.length, "Vita hozzászólás": p.debate?.turns?.length || 0,
      "Kódverzió": p.code.versions.length, "Tesztfuttatás": p.testing.runs.length, "Közös döntés": p.consensus.length,
    };
    root.innerHTML = `<h1>Projekt</h1><p class="subtitle">A teljes állapot (feladat, modellbeállítások, beszélgetések, vita, terv, kód, tesztek, eredmények, módosítások, végső eredmény) egy JSON dokumentumban. Automatikus mentés 10 másodpercenként és minden folyamat végén.</p>
      <div class="card"><div class="row">
        <label class="field"><span>Projekt neve</span><input type="text" id="proj-name" value="${esc(p.name)}"></label>
        <button class="btn primary" id="proj-save" style="margin-top:10px">💾 Mentés</button>
        <button class="btn" id="proj-new" style="margin-top:10px">+ Új projekt</button>
      </div>
      <div class="stats mt">${Object.entries(counts).map(([k, v]) => `<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("")}</div>
      <div class="hint">Azonosító: <span class="mono">${esc(p.id)}</span> · létrehozva: ${fmtDate(p.created)} · módosítva: ${fmtDate(p.updated)}</div></div>
      <div class="grid2">
        <div class="card"><h2>Exportálás</h2>
          <label class="check"><input type="checkbox" id="proj-keys"> API-kulcsok is kerüljenek bele</label>
          <div class="row mt"><button class="btn" id="proj-export">⤓ Projekt (JSON)</button>
            <a class="btn" href="/api/export/conversation?format=md">⤓ Beszélgetés (Markdown)</a>
            <a class="btn" href="/api/export/conversation?format=json">⤓ Beszélgetés (JSON)</a>
            <a class="btn" href="/api/code/download">⤓ Kód (ZIP)</a></div>
          <h3>Formázott HTML riportok</h3>
          <div class="row">${exportHtmlBtn("all", "⤓ Teljes riport")}
            ${[["arena", "Aréna"], ["debate", "Vita"], ["design", "Tervezés"], ["testing", "Tesztelés"], ["consensus", "Közös döntés"], ["code", "Kód"], ["pipeline", "Teljes folyamat"]]
              .map(([s, l]) => exportHtmlBtn(s, "⤓ " + l)).join("")}</div></div>
        <div class="card"><h2>Importálás</h2>
          <p class="hint">Korábban exportált <span class="mono">.arena.json</span> fájl visszatöltése (az aktuális projekt előtte automatikusan mentődik).</p>
          <input type="file" id="proj-file" accept=".json,application/json"></div>
      </div>
      <div class="card"><div class="card-head"><h2>Mentett projektek</h2><span class="spacer"></span><button class="btn small" id="proj-refresh">↻</button></div><div id="proj-list"></div></div>`;
    root.querySelector("#proj-save").onclick = async () => {
      try { const r = await api.saveProject(root.querySelector("#proj-name").value); toast("Mentve: " + r.path, "ok"); refresh(10); loadList(); } catch (e) { toast(e.message, "error"); }
    };
    root.querySelector("#proj-new").onclick = async () => {
      const name = prompt("Új projekt neve:", "");
      if (name === null) return;
      try { await api.newProject(name); await reloadAll("Új projekt létrehozva"); } catch (e) { toast(e.message, "error"); }
    };
    root.querySelector("#proj-export").onclick = () => {
      location.href = "/api/project/export?include_keys=" + (root.querySelector("#proj-keys").checked ? "1" : "0");
    };
    root.querySelector("#proj-refresh").onclick = loadList;
    root.querySelector("#proj-file").onchange = async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      try {
        const data = JSON.parse(await file.text());
        await api.importProject(data);
        await reloadAll("Projekt importálva: " + (data.name || file.name));
      } catch (err) { toast("Importálás sikertelen: " + err.message, "error"); }
    };
    root.addEventListener("click", async (e) => {
      const l = e.target.closest("[data-load]");
      if (l) { try { await api.loadProject(l.dataset.load); await reloadAll("Projekt betöltve"); } catch (err) { toast(err.message, "error"); } return; }
      const d = e.target.closest("[data-del]");
      if (d && confirm("Végleg törlöd a mentett projektet?")) {
        try { await api.deleteProject(d.dataset.del); loadList(); } catch (err) { toast(err.message, "error"); }
      }
    });
    loadList();
  },
  update() { /* static */ },
};
