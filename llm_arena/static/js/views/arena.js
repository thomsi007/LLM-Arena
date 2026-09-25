// Arena: same prompt to both models, side by side, multi-round.
import { api } from "../api.js";
import { state, trackJob, messageById, runningJobs } from "../state.js";
import { esc, toast, messageCard, runningBanner, fmtTime, fmtSec, exportHtmlBtn } from "../ui.js";

let root;

function roundHtml(r) {
  const a = messageById(r.responses.A);
  const b = messageById(r.responses.B);
  let cmp = "";
  if (a?.status === "done" && b?.status === "done") {
    const faster = a.latency <= b.latency ? "A" : "B";
    const diff = Math.abs(a.latency - b.latency);
    cmp = `<span class="hint">Gyorsabb: LLM ${faster} (${fmtSec(diff)} különbség) · tokenek: A ${a.tokens} / B ${b.tokens}</span>`;
  }
  const busy = runningJobs().length > 0;
  return `<div class="round">
    <div class="round-head"><span class="badge">${r.round}. kör${r.kind === "analysis" ? " · elemzés" : ""}</span>
      <div class="prompt">${esc(r.prompt)}</div></div>
    <div class="versus">
      ${messageCard(a || null, { slot: "A", placeholder: "Nincs válasz", retry: { action: "retry", round: r.round } })}
      ${messageCard(b || null, { slot: "B", placeholder: "Nincs válasz", retry: { action: "retry", round: r.round } })}
    </div>
    <div class="row mt">${cmp}<span class="spacer"></span>
      <span class="hint">${fmtTime(r.created)}</span>
      <button class="btn small" data-action="consensus" data-round="${r.round}" ${a?.status === "done" && b?.status === "done" && !busy ? "" : "disabled"}>⚖ Közös döntés erről a körről</button>
    </div></div>`;
}

async function send() {
  const ta = root.querySelector("#arena-prompt");
  const prompt = ta.value.trim();
  if (!prompt) { toast("Írj be egy feladatot vagy kérdést.", "warn"); return; }
  try {
    const r = await api.arena(prompt, root.querySelector("#arena-multi").checked);
    trackJob(r.job);
    ta.value = "";
  } catch (e) { toast(e.message, "error"); }
}

export default {
  mount(el) {
    root = el;
    root.innerHTML = `<h1>Aréna</h1>
      <p class="subtitle">Ugyanaz a feladat mindkét modellnek, párhuzamosan. Több körös összehasonlítás: a korábbi körök a modellek saját beszélgetési előzményeként mennek tovább.</p>
      <div class="card">
        <label class="field"><span>Feladat / kérdés (Ctrl+Enter = küldés)</span>
          <textarea id="arena-prompt" rows="4" placeholder="pl. Hasonlítsd össze a REST és a gRPC előnyeit egy belső mikroszolgáltatás-rendszerben."></textarea></label>
        <div class="row">
          <label class="check"><input type="checkbox" id="arena-multi" checked> Több körös (előzmények megtartása)</label>
          <span class="spacer"></span>
          <button class="btn ghost" id="arena-clear">Körök törlése</button>
          ${exportHtmlBtn("arena")}
          <a class="btn ghost" href="/api/export/conversation?format=md">⤓ Beszélgetés (MD)</a>
          <a class="btn ghost" href="/api/export/conversation?format=json">⤓ JSON</a>
          <button class="btn primary" id="arena-send">▶ Küldés mindkét modellnek</button>
        </div>
      </div>
      <div id="arena-banner"></div>
      <div id="arena-rounds"></div>`;
    root.querySelector("#arena-send").onclick = send;
    root.querySelector("#arena-prompt").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
    });
    root.querySelector("#arena-clear").onclick = async () => {
      if (!confirm("Törlöd az összes aréna kört? (Az üzenetek a naplóban/exportban megmaradnak.)")) return;
      await api.arenaClear();
      state.project.arena.rounds = [];
      this.update("project");
    };
    root.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-action]");
      if (!b) return;
      try {
        if (b.dataset.action === "retry") {
          const r = await api.arenaRetry(+b.dataset.round, b.dataset.slot);
          trackJob(r.job);
        } else if (b.dataset.action === "consensus") {
          const r = await api.consensus({ round: +b.dataset.round });
          trackJob(r.job);
          toast("Közös döntés elindítva – eredmény a „Közös döntés” fülön.", "ok");
        }
      } catch (err) { toast(err.message, "error"); }
    });
  },
  update(reason) {
    root.querySelector("#arena-banner").innerHTML = runningBanner(["arena", "consensus"]);
    if (reason === "jobs") return;
    const rounds = state.project.arena.rounds;
    root.querySelector("#arena-rounds").innerHTML = rounds.length
      ? [...rounds].reverse().map(roundHtml).join("")
      : '<div class="empty">Még nincs kör. Írj be egy feladatot és küldd el mindkét modellnek.</div>';
  },
};
