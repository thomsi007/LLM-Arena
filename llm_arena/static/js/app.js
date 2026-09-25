// App shell: navigation, top bar (connection pills, running jobs), global actions.
import { api } from "./api.js";
import { state, subscribe, refresh, trackJob, onToken } from "./state.js";
import { esc, toast, copyText, updateLive } from "./ui.js";
import settings from "./views/settings.js";
import arena from "./views/arena.js";
import debate from "./views/debate.js";
import design from "./views/design.js";
import code from "./views/code.js";
import testing from "./views/testing.js";
import consensus from "./views/consensus.js";
import pipeline from "./views/pipeline.js";
import log from "./views/log.js";
import project from "./views/project.js";

const VIEWS = { settings, arena, debate, design, code, testing, consensus, pipeline, log, project };
const main = document.getElementById("main");
let current = null;

function show(tab) {
  if (!VIEWS[tab]) tab = "arena";
  state.tab = tab;
  try { localStorage.setItem("arena.tab", tab); } catch { /* ignore */ }
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  if (current?.unmount) current.unmount();
  current = VIEWS[tab];
  main.innerHTML = "";
  main.scrollTop = 0;
  if (state.project) {
    current.mount(main);
    current.update("mount");
  } else {
    main.innerHTML = '<div class="empty">Betöltés…</div>';
  }
  if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
}

document.addEventListener("click", (e) => {
  const nav = e.target.closest("[data-tab]");
  if (nav && (nav.closest("#nav") || nav.classList.contains("pill") || nav.dataset.goto !== undefined)) {
    e.preventDefault();
    show(nav.dataset.tab);
    return;
  }
  const cc = e.target.closest("[data-copy-code]");
  if (cc) { copyText(cc.parentElement.querySelector("code").textContent); return; }
  const cm = e.target.closest("[data-copy-msg]");
  if (cm) {
    const m = state.project.messages.find((x) => x.id === cm.dataset.copyMsg);
    if (m) copyText(m.content);
    return;
  }
  const cj = e.target.closest("[data-cancel-job]");
  if (cj) {
    api.cancel(cj.dataset.cancelJob).then(() => toast("Megszakítás kérve", "warn")).catch((err) => toast(err.message, "error"));
  }
});

// ---------------------------------------------------------------- top bar
function renderTop() {
  const p = state.project;
  if (!p) return;
  document.getElementById("project-name").textContent = p.name;
  for (const slot of ["A", "B"]) {
    const pill = document.getElementById("pill-" + slot);
    const c = state.conn[slot];
    const cfg = p.llms[slot];
    pill.classList.toggle("ok", !!(c && c.ok));
    pill.classList.toggle("err", !!(c && !c.ok));
    pill.querySelector("span").textContent = `${cfg.name || "LLM " + slot}: ${c?.model || cfg.model || (c && !c.ok ? "nem elérhető" : "auto")}`;
    pill.title = `${cfg.base_url}${c?.error ? " – " + c.error : ""}`;
  }
  const running = [...state.jobs.values()].filter((j) => j.status === "running");
  document.getElementById("jobs").innerHTML = running.map((j) => `
    <div class="job-chip"><div class="label"><div><strong>${esc(j.title)}</strong></div>
      <div class="stage">${esc(j.stage || "…")}</div>
      <div class="progress ${j.progress > 0 ? "" : "indeterminate"}"><div style="width:${Math.round((j.progress || 0) * 100)}%"></div></div></div>
      <button class="btn small danger" data-cancel-job="${j.id}" title="Megszakítás">■</button></div>`).join("");
}

export async function probeConnections() {
  for (const slot of ["A", "B"]) {
    api.detectModels(slot).then((r) => {
      state.conn[slot] = r.ok ? { ok: true, model: r.model } : { ok: false, error: r.error?.message };
      renderTop();
    }).catch((e) => { state.conn[slot] = { ok: false, error: e.message }; renderTop(); });
  }
}

let lastReason = 0;
subscribe((reason) => {
  renderTop();
  if (!current) return;
  // Throttle "jobs" (progress) updates for views.
  if (reason === "jobs") {
    const now = performance.now();
    if (now - lastReason < 250) return;
    lastReason = now;
  }
  try { current.update(reason); } catch (e) { console.error(e); }
});

onToken((id) => updateLive(id));

// ------------------------------------------------------------------ theme
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("arena.theme", t); } catch { /* ignore */ }
}
document.getElementById("theme-toggle").onclick = () =>
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
try {
  const saved = localStorage.getItem("arena.theme");
  applyTheme(saved || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
} catch { applyTheme("dark"); }

// ------------------------------------------------------------------- boot
async function boot() {
  window.__arenaBooted = true;
  let tab = location.hash.slice(1);
  try { tab = tab || localStorage.getItem("arena.tab") || "arena"; } catch { tab = tab || "arena"; }
  state.tab = tab;
  try {
    const data = await api.project();
    state.project = data.project;
    for (const j of data.jobs) trackJob(j);
  } catch (e) {
    main.innerHTML = `<div class="errbox">${esc(e.message)}</div>`;
    return;
  }
  renderTop();
  show(tab);
  probeConnections();
}
window.addEventListener("hashchange", () => { const t = location.hash.slice(1); if (t && t !== state.tab) show(t); });
window.addEventListener("focus", () => refresh(10));
boot();
export { show };
