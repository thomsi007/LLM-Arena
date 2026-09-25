// Thin client for the backend REST + SSE API.

/** Error carrying the server's structured description ({label, message, hint, detail}). */
export class ApiError extends Error {
  constructor(info) {
    super(info.message || info.label || "Ismeretlen hiba");
    this.info = info;
  }
}

async function request(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    throw new ApiError({
      kind: "local_server", label: "A helyi szerver nem érhető el",
      message: `A böngésző nem tudott kapcsolódni az LLM Aréna szerveréhez (${e.message}).`,
      hint: "Fut még a „python -m llm_arena” parancs? Nézd meg a konzolablakot (hibaüzenet?), szükség esetén indítsd újra, majd frissítsd az oldalt.",
    });
  }
  const raw = await res.text();
  let data = null;
  try { data = raw ? JSON.parse(raw) : null; } catch { /* non JSON */ }
  if (!res.ok || (data && data.ok === false)) {
    if (data && data.error_info) throw new ApiError(data.error_info);
    throw new ApiError({
      kind: "http", label: `A szerver hibát adott (HTTP ${res.status})`,
      message: (data && data.error) || raw.slice(0, 300) || res.statusText || "ismeretlen hiba",
      hint: res.status >= 500 ? "Nézd meg a szerver konzolját és a Napló fület a részletekért." : "",
      detail: raw.slice(0, 4000),
    });
  }
  return data;
}

/** Read a File as base64 (without the data: prefix). */
export function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => reject(new ApiError({ kind: "attachment", label: "A fájl nem olvasható",
      message: `${file.name}: ${r.error?.message || "olvasási hiba"}`, hint: "Ellenőrizd, hogy a fájl nincs-e megnyitva/zárolva." }));
    r.readAsDataURL(file);
  });
}

export const api = {
  get: (url) => request("GET", url),
  post: (url, body = {}) => request("POST", url, body),

  project: () => request("GET", "/api/project"),
  config: () => request("GET", "/api/config"),
  saveConfig: (data) => request("POST", "/api/config", data),
  testLLM: (slot, config) => request("POST", `/api/llm/${slot}/test`, { config }),
  searxngStatus: () => request("GET", "/api/searxng/status"),
  searxngStart: () => request("POST", "/api/searxng/start"),
  searxngStop: () => request("POST", "/api/searxng/stop"),
  searxngDetect: () => request("POST", "/api/searxng/detect", { apply: true }),
  browserTest: () => request("POST", "/api/web/browser-test"),
  webCacheClear: () => request("POST", "/api/web/cache-clear"),
  webTest: (query) => request("POST", "/api/web/test", { query }),
  detectModels: (slot, config) => request("POST", `/api/llm/${slot}/models`, { config }),

  arena: (prompt, multi_turn, attachments) => request("POST", "/api/arena/run", { prompt, multi_turn, attachments }),
  arenaRetry: (round, slot) => request("POST", "/api/arena/retry", { round, slot }),
  arenaClear: () => request("POST", "/api/arena/clear"),
  consensus: (body) => request("POST", "/api/consensus/run", body),
  debate: (body) => request("POST", "/api/debate/start", body),
  design: (body) => request("POST", "/api/design/start", body),
  testing: (action, body = {}) => request("POST", `/api/testing/${action}`, body),
  pipeline: (body) => request("POST", "/api/pipeline/start", body),

  uploadAttachment: async (file) => request("POST", "/api/attachments", { name: file.name, mime: file.type, data: await fileToBase64(file) }),
  deleteAttachment: (id) => request("POST", `/api/attachments/${id}/delete`),
  uploadCode: async (file, name) => request("POST", "/api/code/upload", { name: name || file.name, data: await fileToBase64(file) }),

  cancel: (id) => request("POST", `/api/jobs/${id}/cancel`),
  cancelAll: () => request("POST", "/api/jobs/cancel-all"),

  projects: () => request("GET", "/api/projects"),
  newProject: (name) => request("POST", "/api/project/new", { name }),
  saveProject: (name) => request("POST", "/api/project/save", { name }),
  loadProject: (id) => request("POST", "/api/project/load", { id }),
  deleteProject: (id) => request("POST", "/api/project/delete", { id }),
  importProject: (data) => request("POST", "/api/project/import", data),
  editCode: (files, deleted, note) => request("POST", "/api/code/edit", { files, deleted, note }),
  log: (since) => request("GET", `/api/log?since=${since || 0}`),
  clearLog: () => request("POST", "/api/log/clear"),

  /** Subscribe to a job's event stream. Returns a close() function. */
  stream(jobId, onEvent, since = -1) {
    let last = since;
    let es = null;
    let closed = false;
    let retries = 0;
    const open = () => {
      es = new EventSource(`/api/jobs/${jobId}/events?since=${last}`);
      es.onmessage = (m) => {
        retries = 0;
        let evt;
        try { evt = JSON.parse(m.data); } catch { return; }
        last = evt.seq;
        onEvent(evt);
        if (evt.type === "job_end") close();
      };
      es.onerror = () => {
        es.close();
        if (closed) return;
        // Reconnect manually (keeps our own "since" cursor).
        if (retries++ < 20) setTimeout(open, Math.min(500 * retries, 4000));
        else onEvent({ type: "stream_lost", job: jobId });
      };
    };
    const close = () => { closed = true; if (es) es.close(); };
    open();
    return close;
  },
};
