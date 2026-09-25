// Shared UI helpers and components.
import { esc, markdown } from "./md.js";
import { state, messageById } from "./state.js";

export { esc, markdown };

export function toast(text, type = "info", ms = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = text;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast("Vágólapra másolva", "ok", 1500);
}

export function download(name, content, mime = "text/plain") {
  const blob = content instanceof Blob ? content : new Blob([content], { type: mime + ";charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}

export const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString("hu-HU") : "");
export const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString("hu-HU") : "");
export const fmtSec = (s) => (s == null ? "–" : s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(2)} s`);

export const slotBadge = (slot) => `<span class="badge ${String(slot).toLowerCase()}">LLM ${esc(slot)}</span>`;
const STATUS_HU = {
  done: "kész", running: "fut", error: "hiba", cancelled: "megszakítva", pending: "várakozik", streaming: "streaming",
  passed: "sikeres", failed: "bukott", timeout: "időtúllépés", skipped: "kihagyva", no_change: "nincs változás",
  addressed: "javítva", open: "nyitott",
};
export const statusBadge = (s) => `<span class="badge ${esc(s)}">${esc(STATUS_HU[s] || s)}</span>`;

export function llmName(slot) {
  const c = state.project?.llms?.[slot];
  return c?.name || `LLM ${slot}`;
}

/** Render a model answer card. `msg` may be an id or a message object. */
export function messageCard(msgOrId, opts = {}) {
  const msg = typeof msgOrId === "string" ? messageById(msgOrId) : msgOrId;
  if (!msg) {
    return opts.placeholder ? `<div class="msg slot-${opts.slot}"><div class="msg-head">${slotBadge(opts.slot)}</div>
      <div class="msg-body"><div class="empty">${esc(opts.placeholder)}</div></div></div>` : "";
  }
  const live = state.live.get(msg.id);
  const streaming = msg.status === "streaming" || !!live;
  const content = live ? live.content : (opts.content ?? msg.content);
  const reasoning = live ? live.reasoning : msg.reasoning;
  const err = msg.error;
  const tok = msg.tokens ? `${msg.tokens}${msg.tokens_estimated ? " (becsült)" : ""} token` : "";
  const actions = [];
  if (!streaming && content) actions.push(`<button class="btn small" data-copy-msg="${msg.id}" title="Válasz másolása">Másolás</button>`);
  if (opts.retry && !streaming) actions.push(`<button class="btn small" data-action="${opts.retry.action}" data-round="${opts.retry.round ?? ""}" data-slot="${msg.slot}">↻ Újra</button>`);
  const body = content
    ? `<div class="md" data-live-content="${msg.id}">${markdown(content)}</div>`
    : streaming ? `<div class="md hint" data-live-content="${msg.id}">Várakozás az első tokenre…</div>` : "";
  return `<div class="msg slot-${msg.slot} ${streaming ? "streaming" : ""}" data-msg-id="${msg.id}">
    <div class="msg-head">${slotBadge(msg.slot)}<span class="who">${esc(opts.title || msg.title || llmName(msg.slot))}</span>
      <span class="model" title="${esc(msg.model)}">${esc(msg.model || "")}</span><span class="spacer"></span>
      ${opts.extraHead || ""}${statusBadge(streaming ? "streaming" : msg.status)}</div>
    <div class="msg-body">
      ${reasoning ? `<details class="reasoning"><summary>Gondolatmenet (${reasoning.length} karakter)</summary><pre data-live-reasoning="${msg.id}">${esc(reasoning)}</pre></details>` : ""}
      ${body}
      ${err ? `<div class="errbox"><strong>${esc(err.label || "Hiba")}</strong>: ${esc(err.message)}${err.status ? ` (HTTP ${err.status})` : ""}</div>` : ""}
    </div>
    <div class="msg-meta">
      <span title="Teljes válaszidő">⏱ ${streaming ? "…" : fmtSec(msg.latency)}</span>
      ${msg.ttft != null ? `<span title="Első token ideje">⚡ ${fmtSec(msg.ttft)}</span>` : ""}
      ${tok ? `<span>🔢 ${tok}</span>` : ""}
      ${msg.prompt_tokens != null ? `<span title="Prompt tokenek">↳ ${msg.prompt_tokens} prompt</span>` : ""}
      ${msg.tokens_per_second ? `<span>${msg.tokens_per_second} tok/s</span>` : ""}
      ${msg.attempts > 1 ? `<span title="Próbálkozások">🔁 ${msg.attempts}</span>` : ""}
      ${msg.finish_reason === "length" ? `<span class="badge medium" title="Elérte a max token limitet">levágva</span>` : ""}
      <span class="actions">${actions.join("")}</span>
    </div>
  </div>`;
}

let liveFrame = null;
const dirtyLive = new Set();
/** Called by the app on each token: re-render streaming messages (rAF throttled). */
export function updateLive(msgId) {
  dirtyLive.add(msgId);
  if (liveFrame) return;
  liveFrame = requestAnimationFrame(() => {
    liveFrame = null;
    for (const id of dirtyLive) {
      const l = state.live.get(id);
      if (!l) continue;
      document.querySelectorAll(`[data-live-content="${id}"]`).forEach((el) => {
        el.classList.remove("hint");
        el.innerHTML = markdown(l.content) || '<span class="hint">…</span>';
      });
      if (l.reasoning) {
        const pres = document.querySelectorAll(`[data-live-reasoning="${id}"]`);
        if (pres.length) pres.forEach((el) => { el.textContent = l.reasoning; });
        else {
          const card = document.querySelector(`[data-msg-id="${id}"] .msg-body`);
          if (card && !card.querySelector(".reasoning")) {
            card.insertAdjacentHTML("afterbegin", `<details class="reasoning"><summary>Gondolatmenet…</summary><pre data-live-reasoning="${id}"></pre></details>`);
            card.querySelector("pre[data-live-reasoning]").textContent = l.reasoning;
          }
        }
      }
    }
    dirtyLive.clear();
  });
}

export function list(items, empty = "–") {
  if (!items || !items.length) return `<div class="hint">${esc(empty)}</div>`;
  return `<ul class="clean">${items.map((i) => `<li>${esc(typeof i === "string" ? i : JSON.stringify(i))}</li>`).join("")}</ul>`;
}

export function jsonBlock(obj) {
  return `<div class="kv">${esc(JSON.stringify(obj, null, 2))}</div>`;
}

export function jobsFor(kind) {
  return [...state.jobs.values()].filter((j) => j.kind === kind && j.status === "running");
}

/** A small "running" banner with cancel button for a view. */
export function runningBanner(kinds) {
  const jobs = [...state.jobs.values()].filter((j) => j.status === "running" && kinds.includes(j.kind));
  if (!jobs.length) return "";
  return jobs.map((j) => `<div class="card"><div class="row"><strong>${esc(j.title)}</strong>
    <span class="hint">${esc(j.stage || "")}</span><span class="spacer"></span>
    <button class="btn small danger" data-cancel-job="${j.id}">■ Megszakítás</button></div>
    <div class="progress ${j.progress > 0 ? "" : "indeterminate"}"><div style="width:${Math.round((j.progress || 0) * 100)}%"></div></div></div>`).join("");
}

export function lastFinishedError(kind) {
  const jobs = [...state.jobs.values()].filter((j) => j.kind === kind).sort((a, b) => b.created - a.created);
  const j = jobs[0];
  if (j && j.status === "error" && j.error) {
    return `<div class="errbox"><strong>${esc(j.error.label || "Hiba")}</strong>: ${esc(j.error.message)}</div>`;
  }
  return "";
}

export const CRITERIA = ["correctness", "completeness", "feasibility", "security", "performance", "testability"];
export const CRITERIA_HU = {
  correctness: "Helyesség", completeness: "Teljesség", feasibility: "Megvalósíthatóság",
  security: "Biztonság", performance: "Teljesítmény", testability: "Tesztelhetőség",
};
