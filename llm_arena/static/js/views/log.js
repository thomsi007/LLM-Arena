// Log viewer with level / text filter and live polling.
import { api } from "../api.js";
import { state } from "../state.js";
import { esc, toast, download } from "../ui.js";

let root;
let timer = null;
let entries = [];
let lastTs = 0;

function render() {
  const lv = root.querySelector("#log-level").value;
  const q = root.querySelector("#log-q").value.toLowerCase();
  const rows = entries.filter((e) => (!lv || e.level === lv || (lv === "warning" && e.level === "error")) && (!q || (e.text + e.source).toLowerCase().includes(q)));
  const box = root.querySelector("#log-list");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.innerHTML = rows.map((e) => `<div class="log-row"><span>${new Date(e.ts * 1000).toLocaleTimeString("hu-HU")}</span>
    <span class="lv-${esc(e.level)}">${esc(e.level)}</span><span class="src">${esc(e.source)}</span><span>${esc(e.text)}</span></div>`).join("") ||
    '<div class="hint" style="padding:10px">Nincs bejegyzés.</div>';
  if (atBottom) box.scrollTop = box.scrollHeight;
  root.querySelector("#log-count").textContent = `${rows.length} / ${entries.length} bejegyzés`;
}

async function poll() {
  try {
    const r = await api.log(lastTs);
    if (r.log.length) {
      entries = entries.concat(r.log).slice(-3000);
      lastTs = entries[entries.length - 1].ts;
      render();
    }
  } catch { /* server gone */ }
}

export default {
  mount(el) {
    root = el;
    entries = [];
    lastTs = 0;
    root.innerHTML = `<h1>Napló</h1><p class="subtitle">Minden modellhívás, hiba, újrapróbálás, teszteredmény és mentés.</p>
      <div class="card"><div class="row">
        <label class="field" style="max-width:160px"><span>Szint</span><select id="log-level"><option value="">mind</option><option value="info">info</option><option value="warning">figyelmeztetés+</option><option value="error">hiba</option></select></label>
        <label class="field"><span>Keresés</span><input type="text" id="log-q" placeholder="szűrés…"></label>
        <span class="hint" id="log-count"></span><span class="spacer"></span>
        <button class="btn" id="log-dl">⤓ Letöltés</button><button class="btn danger" id="log-clear">Törlés</button>
      </div></div><div class="log-list" id="log-list"></div>`;
    root.querySelector("#log-level").onchange = render;
    root.querySelector("#log-q").oninput = render;
    root.querySelector("#log-dl").onclick = () => download(`${state.project.name}_log.txt`,
      entries.map((e) => `${new Date(e.ts * 1000).toISOString()}\t${e.level}\t${e.source}\t${e.text}`).join("\n"));
    root.querySelector("#log-clear").onclick = async () => {
      if (!confirm("Törlöd a naplót?")) return;
      try { await api.clearLog(); entries = []; render(); } catch (e) { toast(e.message, "error"); }
    };
    poll();
    timer = setInterval(poll, 2000);
  },
  update(reason) { if (reason === "log") poll(); },
  unmount() { clearInterval(timer); },
};
