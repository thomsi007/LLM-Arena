// Client state + tiny pub/sub. The backend project JSON is the source of truth;
// streaming tokens are buffered in `live` until the message is finished.
import { api } from "./api.js";

export const state = {
  project: null,
  jobs: new Map(),       // jobId -> {id, kind, title, status, progress, stage, error}
  live: new Map(),       // msgId -> {content, reasoning, status}
  conn: { A: null, B: null },
  tab: "arena",
  drafts: {},            // section -> [attachment meta] selected for the next run
};

const listeners = new Set();
export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function notify(reason) { for (const fn of listeners) { try { fn(reason); } catch (e) { console.error(e); } } }

const tokenListeners = new Set();
export function onToken(fn) { tokenListeners.add(fn); return () => tokenListeners.delete(fn); }

let refreshTimer = null;
let refreshing = false;
let again = false;
export function refresh(delay = 250) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    if (refreshing) { again = true; return; }
    refreshing = true;
    try {
      const data = await api.project();
      state.project = data.project;
      for (const j of data.jobs) {
        const cur = state.jobs.get(j.id);
        state.jobs.set(j.id, { ...(cur || {}), ...j });
      }
      notify("project");
    } catch (e) {
      console.warn(e);
    } finally {
      refreshing = false;
      if (again) { again = false; refresh(100); }
    }
  }, delay);
}

export function messageById(id) {
  if (!id || !state.project) return null;
  const msgs = state.project.messages;
  for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].id === id) return msgs[i];
  return null;
}

const streams = new Map();

export function trackJob(job) {
  state.jobs.set(job.id, { ...job });
  if (streams.has(job.id) || ["done", "error", "cancelled"].includes(job.status)) { notify("jobs"); return; }
  const close = api.stream(job.id, (evt) => handleEvent(job.id, evt));
  streams.set(job.id, close);
  notify("jobs");
}

function handleEvent(jobId, evt) {
  const job = state.jobs.get(jobId) || { id: jobId };
  switch (evt.type) {
    case "progress":
      job.progress = evt.value;
      if (evt.label) job.stage = evt.label;
      notify("jobs");
      break;
    case "msg_start":
      state.live.set(evt.msg_id, { content: "", reasoning: "", status: "streaming" });
      refresh(80);
      break;
    case "token": {
      const l = state.live.get(evt.msg_id) || { content: "", reasoning: "", status: "streaming" };
      if (evt.kind === "reasoning") l.reasoning += evt.delta; else l.content += evt.delta;
      state.live.set(evt.msg_id, l);
      for (const fn of tokenListeners) fn(evt.msg_id, l);
      break;
    }
    case "msg_reset": {
      state.live.set(evt.msg_id, { content: "", reasoning: "", status: "streaming" });
      for (const fn of tokenListeners) fn(evt.msg_id, state.live.get(evt.msg_id));
      break;
    }
    case "msg_end":
    case "msg_error":
      state.live.delete(evt.msg_id || (evt.message && evt.message.id));
      refresh(60);
      break;
    case "state":
      refresh(200);
      break;
    case "tool":
      refresh(60);
      break;
    case "log":
      notify("log");
      break;
    case "job_end":
      job.status = evt.status;
      job.error = evt.error;
      job.progress = evt.status === "done" ? 1 : job.progress;
      streams.delete(jobId);
      refresh(50);
      notify("job_end:" + jobId);
      break;
    case "stream_lost":
      streams.delete(jobId);
      refresh(50);
      break;
  }
  state.jobs.set(jobId, job);
}

export function runningJobs(kind) {
  return [...state.jobs.values()].filter((j) => j.status === "running" && (!kind || j.kind === kind));
}

/** Resolve when a job finishes. */
export function waitJob(jobId) {
  return new Promise((resolve) => {
    const un = subscribe((reason) => {
      if (reason === "job_end:" + jobId) { un(); resolve(state.jobs.get(jobId)); }
    });
  });
}
