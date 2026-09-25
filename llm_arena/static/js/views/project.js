// Session / project management: save, load, new, export, import.
import { api } from "../api.js";
import { state, refresh } from "../state.js";
import { esc, toast, fmtDate, exportHtmlBtn, showError, errorBox, attachmentChip, fmtSize } from "../ui.js";
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
  } catch (e) { showError(e, "Projekt"); }
}

function renderAtts() {
  const atts = state.project.attachments || [];
  root.querySelector("#proj-atts").innerHTML = atts.length
    ? `<table class="tbl"><thead><tr><th>Fájl</th><th>Típus</th><th>Méret</th><th>Tartalom</th><th></th></tr></thead><tbody>
      ${atts.map((a) => `<tr><td>${attachmentChip(a)}</td><td>${esc(a.kind === "image" ? "kép" : a.ext || "szöveg")}</td><td>${fmtSize(a.size)}</td>
        <td class="hint">${a.kind === "image" ? "kép (multimodális modellnek)" : `${a.chars.toLocaleString("hu-HU")} karakter${a.truncated ? " (levágva)" : ""}`}${a.note ? " · " + esc(a.note) : ""}</td>
        <td style="white-space:nowrap">${a.kind === "text" ? `<a class="btn small" href="/api/attachments/${a.id}?text=1" target="_blank">Kinyert szöveg</a>` : ""}
          <button class="btn small danger" data-att-del="${a.id}">Törlés</button></td></tr>`).join("")}</tbody></table>`
    : '<div class="hint">Nincs csatolt fájl.</div>';
}

async function reloadAll(msg) {
  const data = await api.project();
  state.project = data.project;
  state.live.clear();
  state.drafts = {};
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
      <div class="card"><div class="card-head"><h2>📎 Csatolt fájlok</h2><span class="hint">A projekthez feltöltött összes fájl (az Aréna, Vita, Tervezés és Teljes folyamat fülön csatolhatók)</span></div>
        <div id="proj-atts"></div></div>
      <div class="card"><div class="card-head"><h2>Mentett projektek</h2><span class="spacer"></span><button class="btn small" id="proj-refresh">↻</button></div><div id="proj-list"></div></div>`;
    root.querySelector("#proj-save").onclick = async () => {
      try { const r = await api.saveProject(root.querySelector("#proj-name").value); toast("Mentve: " + r.path, "ok"); refresh(10); loadList(); } catch (e) { showError(e, "Projekt"); }
    };
    root.querySelector("#proj-new").onclick = async () => {
      const name = prompt("Új projekt neve:", "");
      if (name === null) return;
      try { await api.newProject(name); await reloadAll("Új projekt létrehozva"); } catch (e) { showError(e, "Projekt"); }
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
      } catch (err) { showError(err, "Projekt importálása"); }
    };
    root.addEventListener("click", async (e) => {
      const l = e.target.closest("[data-load]");
      if (l) { try { await api.loadProject(l.dataset.load); await reloadAll("Projekt betöltve"); } catch (err) { showError(err, "Projekt"); } return; }
      const ad = e.target.closest("[data-att-del]");
      if (ad) {
        if (!confirm("Törlöd a fájlt? A korábbi futások hivatkozása „törölt fájl” lesz.")) return;
        try {
          await api.deleteAttachment(ad.dataset.attDel);
          state.project.attachments = state.project.attachments.filter((a) => a.id !== ad.dataset.attDel);
          for (const k of Object.keys(state.drafts)) state.drafts[k] = state.drafts[k].filter((a) => a.id !== ad.dataset.attDel);
          renderAtts();
        } catch (err) { showError(err, "Fájl törlése"); }
        return;
      }
      const d = e.target.closest("[data-del]");
      if (d && confirm("Végleg törlöd a mentett projektet?")) {
        try { await api.deleteProject(d.dataset.del); loadList(); } catch (err) { showError(err, "Projekt"); }
      }
    });
    loadList();
    renderAtts();
  },
  update() { /* static */ },
};
