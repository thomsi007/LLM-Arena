// Shared UI helpers and components.
import { esc, markdown } from "./md.js";
import { state, messageById } from "./state.js";
import { api } from "./api.js";

export { esc, markdown };

export function toast(text, type = "info", ms = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = text;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

/** Normalize anything thrown / returned into {label, message, hint, detail}. */
export function errorInfo(err, context = "") {
  let info;
  if (!err) info = { label: "Ismeretlen hiba", message: "" };
  else if (err.info) info = { ...err.info };
  else if (err instanceof Error) info = { label: "Hiba a felületen", message: err.message, detail: err.stack, hint: "Frissítsd az oldalt (Ctrl+F5). Ha ismétlődik, küldd el a részleteket." };
  else if (typeof err === "string") info = { label: "Hiba", message: err };
  else info = { ...err };
  if (context) info.context = context;
  return info;
}

function errorInner(info) {
  const details = [info.detail, info.stage ? `Lépés: ${info.stage}` : "", info.kind ? `Típus: ${info.kind}` : "",
    info.status ? `HTTP státusz: ${info.status}` : ""].filter(Boolean).join("\n");
  return `<div class="err-title">⚠ ${esc(info.context ? info.context + " – " : "")}${esc(info.label || "Hiba")}</div>
    ${info.message ? `<div class="err-msg">${esc(info.message)}</div>` : ""}
    ${info.hint ? `<div class="err-hint">💡 ${esc(info.hint)}</div>` : ""}
    ${details ? `<details class="err-details"><summary>Részletek</summary><pre>${esc(details)}</pre></details>` : ""}`;
}

/** Inline error box (inside a view). */
export function errorBox(err, context = "") {
  if (!err) return "";
  return `<div class="errbox">${errorInner(errorInfo(err, context))}</div>`;
}

const recentErrors = new Map();
/** Persistent error notification: stays until closed, with hint, details and copy. */
export function showError(err, context = "") {
  const info = errorInfo(err, context);
  const key = `${info.context}|${info.label}|${info.message}`;
  const now = Date.now();
  if (recentErrors.get(key) > now - 3000) return;
  recentErrors.set(key, now);
  const box = document.getElementById("toasts");
  while (box.querySelectorAll(".toast.error").length >= 4) box.querySelector(".toast.error").remove();
  const el = document.createElement("div");
  el.className = "toast error persistent";
  el.innerHTML = `<button class="toast-close" title="Bezárás">×</button>${errorInner(info)}
    <div class="row end"><button class="btn small" data-copy-err>Másolás</button></div>`;
  el.querySelector(".toast-close").onclick = () => el.remove();
  el.querySelector("[data-copy-err]").onclick = () => copyText(
    [info.context, info.label, info.message, info.hint && "Tipp: " + info.hint, info.detail].filter(Boolean).join("\n"));
  box.appendChild(el);
  console.error("[LLM Aréna]", info);
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
  const tools = toolCallsHtml(msg.tool_calls);
  return `<div class="msg slot-${msg.slot} ${streaming ? "streaming" : ""}" data-msg-id="${msg.id}">
    <div class="msg-head">${slotBadge(msg.slot)}<span class="who">${esc(opts.title || msg.title || llmName(msg.slot))}</span>
      <span class="model" title="${esc(msg.model)}">${esc(msg.model || "")}</span><span class="spacer"></span>
      ${opts.extraHead || ""}${statusBadge(streaming ? "streaming" : msg.status)}</div>
    <div class="msg-body">
      ${tools}
      ${reasoning ? `<details class="reasoning"><summary>Gondolatmenet (${reasoning.length} karakter)</summary><pre data-live-reasoning="${msg.id}">${esc(reasoning)}</pre></details>` : ""}
      ${body}
      ${err ? errorBox(err) : ""}
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

const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");

/** Web tool calls of a message: query / URL, status, and clickable sources. */
export function toolCallsHtml(calls) {
  if (!calls || !calls.length) return "";
  const rows = calls.map((c) => {
    const icon = c.name === "web_search" ? "🔎" : "🌐";
    const arg = c.arguments?.query || c.arguments?.url || "";
    const st = c.status === "running" ? '<span class="badge running">keres…</span>'
      : c.status === "error" ? `<span class="badge error" title="${esc(c.error || "")}">hiba</span>` : "";
    const src = (c.sources || []).slice(0, 6).map((s, i) =>
      `<a href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener noreferrer" title="${esc(s.url)}">[${i + 1}] ${esc((s.title || s.url).slice(0, 70))}</a>`).join("");
    return `<div class="tool-call"><div class="tool-head">${icon} <b>${c.name === "web_search" ? "Webes keresés" : "Oldal olvasása"}</b>
      <span class="mono">${esc(arg)}</span> ${st}<span class="hint">${esc(c.status === "done" ? c.summary || "" : "")}</span></div>
      ${src ? `<div class="tool-sources">${src}</div>` : ""}</div>`;
  }).join("");
  return `<details class="tools" ${calls.some((c) => c.status === "running") ? "open" : ""}><summary>🌐 ${calls.length} webes eszközhívás</summary>${rows}</details>`;
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
  if (j && j.status === "error" && j.error) return errorBox(j.error, j.title);
  return "";
}

/** Download link for the styled, self-contained HTML report of a section. */
export function exportHtmlBtn(section, label = "⤓ HTML export") {
  return `<a class="btn" href="/api/export/html?section=${section}" title="Szépen formázott, önálló HTML fájl (böngészőben megnyitható, nyomtatható PDF-be)">${label}</a>`;
}

// ------------------------------------------------------------ attachments
const KIND_ICON = { text: "📄", image: "🖼" };
export const fmtSize = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} kB` : `${(n / 1048576).toFixed(1)} MB`);

export function attachmentChip(a, removable = false) {
  const info = a.kind === "image" ? "kép" : `${a.chars?.toLocaleString("hu-HU") ?? "?"} karakter`;
  const tip = [a.name, fmtSize(a.size || 0), info, a.note, a.preview && "\n" + a.preview.slice(0, 300)].filter(Boolean).join(" · ");
  return `<span class="att-chip ${a.uploading ? "uploading" : ""}" title="${esc(tip)}">${a.uploading ? "⏳" : KIND_ICON[a.kind] || "📎"}
    ${a.id && !a.uploading ? `<a href="/api/attachments/${a.id}" target="_blank" rel="noopener">${esc(a.name)}</a>` : esc(a.name)}
    <span class="hint">${a.uploading ? "feltöltés…" : `${fmtSize(a.size || 0)}${a.truncated ? " · levágva" : ""}`}</span>
    ${removable && !a.uploading ? `<button class="att-x" data-att-remove="${a.id}" title="Eltávolítás">×</button>` : ""}</span>`;
}

/** Chips of attachments referenced by a run (read-only). */
export function attachmentList(ids) {
  if (!ids || !ids.length) return "";
  const all = new Map((state.project.attachments || []).map((a) => [a.id, a]));
  return `<div class="att-list">📎 ${ids.map((id) => all.get(id) ? attachmentChip(all.get(id)) : `<span class="att-chip missing">törölt fájl</span>`).join("")}</div>`;
}

/** Upload area for a section. Use with bindAttach(). */
export function attachBox(section) {
  return `<div class="attach" data-attach="${section}">
    <div class="att-row"><button class="btn small" type="button" data-att-pick>📎 Fájl csatolása</button>
      <span class="hint">vagy húzd ide a fájlokat · szöveg, kód, JSON/CSV, DOCX, XLSX, PPTX, ODT, PDF, képek (max. 20 MB)</span></div>
    <input type="file" multiple hidden data-att-input>
    <div class="att-chips" data-att-chips></div></div>`;
}

export function draftIds(section) {
  return (state.drafts[section] || []).filter((a) => a.id && !a.uploading).map((a) => a.id);
}

export function setDraft(section, ids) {
  const all = new Map((state.project.attachments || []).map((a) => [a.id, a]));
  state.drafts[section] = (ids || []).map((id) => all.get(id)).filter(Boolean);
}

export function bindAttach(root, section) {
  const box = root.querySelector(`[data-attach="${section}"]`);
  if (!box) return;
  const input = box.querySelector("[data-att-input]");
  const render = () => { box.querySelector("[data-att-chips]").innerHTML = (state.drafts[section] || []).map((a) => attachmentChip(a, true)).join(""); };
  const upload = async (files) => {
    for (const file of files) {
      const temp = { name: file.name, size: file.size, uploading: true };
      (state.drafts[section] ||= []).push(temp);
      render();
      try {
        const r = await api.uploadAttachment(file);
        Object.assign(temp, r.attachment, { uploading: false });
        (state.project.attachments ||= []).push(r.attachment);
        if (r.attachment.note) toast(`${r.attachment.name}: ${r.attachment.note}`, "warn", 6000);
      } catch (e) {
        state.drafts[section] = state.drafts[section].filter((x) => x !== temp);
        showError(e, `Csatolás: ${file.name}`);
      }
      render();
    }
  };
  box.querySelector("[data-att-pick]").onclick = () => input.click();
  input.onchange = () => { upload([...input.files]); input.value = ""; };
  const card = box.closest(".card") || box;
  card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("dropping"); });
  card.addEventListener("dragleave", (e) => { if (!card.contains(e.relatedTarget)) card.classList.remove("dropping"); });
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    card.classList.remove("dropping");
    if (e.dataTransfer.files.length) upload([...e.dataTransfer.files]);
  });
  card.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); upload(files); }
  });
  box.addEventListener("click", (e) => {
    const x = e.target.closest("[data-att-remove]");
    if (x) { state.drafts[section] = (state.drafts[section] || []).filter((a) => a.id !== x.dataset.attRemove); render(); }
  });
  render();
}

/** Pick local files and add them to the project code (source or test files). */
export function pickCodeFiles(onDone, { tests = false } = {}) {
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = true;
  input.onchange = async () => {
    let ok = 0;
    for (const f of input.files) {
      let name = f.name;
      if (tests && !/^test_|_test\.py$/.test(name)) name = "test_" + name;
      try { await api.uploadCode(f, name); ok++; } catch (e) { showError(e, `Feltöltés: ${f.name}`); }
    }
    if (ok) toast(`${ok} fájl hozzáadva a kódhoz`, "ok");
    onDone && onDone();
  };
  input.click();
}

export const CRITERIA = ["correctness", "completeness", "feasibility", "security", "performance", "testability"];
export const CRITERIA_HU = {
  correctness: "Helyesség", completeness: "Teljesség", feasibility: "Megvalósíthatóság",
  security: "Biztonság", performance: "Teljesítmény", testability: "Tesztelhetőség",
};
