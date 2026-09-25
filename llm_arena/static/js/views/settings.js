// LLM A / LLM B configuration, connection test, model auto-detection, global settings.
import { api } from "../api.js";
import { state, refresh, trackJob } from "../state.js";
import { esc, toast, showError, errorBox } from "../ui.js";

let root;
let saveTimer = null;

function llmCard(slot) {
  const c = state.project.llms[slot];
  const f = (key, label, type = "text", extra = "") =>
    `<label class="field"><span>${label}</span><input type="${type}" data-slot="${slot}" data-key="${key}" value="${esc(c[key] ?? "")}" ${extra}></label>`;
  return `<div class="card slot-${slot.toLowerCase()}">
    <div class="card-head"><h2>${slot === "A" ? "LLM A / Llama Server A" : "LLM B / Llama Server B"}</h2>
      <span class="spacer"></span><span class="badge ${slot.toLowerCase()}" id="conn-badge-${slot}">nincs tesztelve</span></div>
    ${f("name", "Megjelenített név")}
    ${f("base_url", "URL / endpoint (pl. http://127.0.0.1:8080 vagy …/v1)", "url")}
    <div class="row">
      <label class="field"><span>Modell (üres vagy „auto” = automatikus felismerés)</span>
        <input type="text" list="models-${slot}" data-slot="${slot}" data-key="model" value="${esc(c.model)}" placeholder="auto">
        <datalist id="models-${slot}"></datalist></label>
      <button class="btn" data-detect="${slot}" style="margin-top:10px">🔍 Felismerés</button>
    </div>
    ${f("api_key", "API-kulcs (opcionális)", "password", 'autocomplete="off" placeholder="nincs"')}
    <div class="grid3">
      ${f("timeout", "Timeout (s)", "number", 'min="1" step="1"')}
      ${f("max_tokens", "Max token", "number", 'min="1" step="1"')}
      ${f("retries", "Újrapróbálások", "number", 'min="0" max="5"')}
    </div>
    <label class="field"><span>Temperature: <b id="temp-${slot}">${c.temperature}</b></span>
      <input type="range" min="0" max="2" step="0.05" data-slot="${slot}" data-key="temperature" value="${c.temperature}"></label>
    <label class="field"><span>Rendszerprompt (minden híváshoz hozzáadódik a szerep-prompt előtt)</span>
      <textarea rows="3" data-slot="${slot}" data-key="system_prompt" placeholder="pl. Tömören és pontosan válaszolj.">${esc(c.system_prompt)}</textarea></label>
    <div class="row">
      <label class="check"><input type="checkbox" data-slot="${slot}" data-key="stream" ${c.stream ? "checked" : ""}> Streaming</label>
      <label class="check" title="chat_template_kwargs: enable_thinking=false – gyorsabb válasz Qwen3 és más gondolkodó modelleknél"><input type="checkbox" data-slot="${slot}" data-key="disable_thinking" ${c.disable_thinking ? "checked" : ""}> Gondolkodás kikapcsolása</label>
      <label class="check" title="A rendszer HTTP proxy beállításainak használata"><input type="checkbox" data-slot="${slot}" data-key="use_system_proxy" ${c.use_system_proxy ? "checked" : ""}> Rendszer proxy</label>
      <label class="field" style="max-width:190px"><span>Kontextus-keret (karakter)</span>
        <input type="number" min="2000" step="1000" data-slot="${slot}" data-key="context_chars" value="${c.context_chars}"></label>
      <span class="spacer"></span>
      <button class="btn primary" data-test="${slot}">⚡ Kapcsolat tesztelése</button>
    </div>
    <div class="conn-steps" id="conn-${slot}"></div>
  </div>`;
}

function globalCard() {
  const s = state.project.settings;
  const sel = (key, opts) => `<select data-setting="${key}">${opts.map(([v, l]) => `<option value="${v}" ${String(s[key]) === String(v) ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  return `<div class="card"><div class="card-head"><h2>Általános beállítások</h2></div>
    <div class="grid3">
      <label class="field"><span>Válasz nyelve</span><input type="text" data-setting="language" value="${esc(s.language)}"></label>
      <label class="field"><span>Elsődleges fejlesztő</span>${sel("developer", [["A", "LLM A (B a reviewer)"], ["B", "LLM B (A a reviewer)"]])}</label>
      <label class="field"><span>Moderátor / szintetizáló</span>${sel("moderator", [["A", "LLM A"], ["B", "LLM B"]])}</label>
      <label class="field"><span>Vitakörök száma</span><input type="number" min="1" max="8" data-setting="debate_rounds" value="${s.debate_rounds}"></label>
      <label class="field"><span>Max. javító iteráció</span><input type="number" min="0" max="10" data-setting="max_fix_iterations" value="${s.max_fix_iterations}"></label>
      <label class="field"><span>Teszt timeout (s)</span><input type="number" min="5" max="600" data-setting="test_timeout" value="${s.test_timeout}"></label>
    </div>
    <div class="row">
      <label class="check"><input type="checkbox" data-setting="dual_architecture" ${s.dual_architecture ? "checked" : ""}> Architektúra: mindkét modell javasol + közös döntés</label>
      <label class="check"><input type="checkbox" data-setting="json_repair" ${s.json_repair ? "checked" : ""}> Hibás JSON esetén javítás kérése</label>
    </div>
    <div class="warnbox mt"><label class="check"><input type="checkbox" data-setting="allow_code_execution" ${s.allow_code_execution ? "checked" : ""}>
      <strong>Generált kód futtatásának engedélyezése (tesztelés)</strong></label>
      <div class="hint">A modellek által írt Python kód a gépeden, a te felhasználóddal fut egy ideiglenes mappában (időkorlát, memória- és CPU-limit, tisztított környezet) – ez folyamat-izoláció, nem teljes sandbox. Csak megbízható modellekkel / felügyelt környezetben kapcsold be.</div></div>
  </div>`;
}

function webCard() {
  const s = state.project.settings;
  const sel = (key, opts) => `<select data-setting="${key}">${opts.map(([v, l]) => `<option value="${v}" ${String(s[key]) === String(v) ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  return `<div class="card"><div class="card-head"><h2>🌐 Webes eszközök (élő információ)</h2><span class="spacer"></span>
      <label class="check"><input type="checkbox" data-setting="web_enabled" ${s.web_enabled ? "checked" : ""}> Engedélyezve</label></div>
    <p class="hint">A modellek szükség esetén maguk döntik el, hogy keresnek-e a weben (<code>web_search</code>) vagy elolvasnak-e egy oldalt (<code>fetch_url</code>).
      Aktív: Aréna, Vita, az elemzés és a tervezési lépések. A válaszkártyákon látszik minden keresés és forrás.</p>
    <div class="grid3">
      <label class="field"><span>Keresőmotor</span>${sel("web_backend", [["duckduckgo", "DuckDuckGo (kulcs nélkül)"], ["searxng", "SearXNG (saját példány)"], ["brave", "Brave Search API"]])}</label>
      <label class="field"><span>SearXNG URL</span><input type="url" data-setting="web_searxng_url" value="${esc(s.web_searxng_url)}" placeholder="http://127.0.0.1:8888"></label>
      <label class="field"><span>Brave API-kulcs</span><input type="password" data-setting="web_brave_api_key" value="${esc(s.web_brave_api_key)}" autocomplete="off" placeholder="nincs"></label>
      <label class="field"><span>Találatok száma</span><input type="number" min="1" max="15" data-setting="web_max_results" value="${s.web_max_results}"></label>
      <label class="field"><span>Max. eszközhívás / válasz</span><input type="number" min="0" max="12" data-setting="web_max_calls" value="${s.web_max_calls}"></label>
      <label class="field"><span>Tool-hívás módja</span>${sel("web_tool_mode", [["auto", "Automatikus (natív, ha megy)"], ["native", "Natív (OpenAI tools)"], ["text", "Szöveges (<tool_call>)"]])}</label>
    </div>
    <div class="sx-box">
      <div class="row"><h3 style="margin:0">🔎 Helyi SearXNG (ingyenes, saját metakereső)</h3><span class="spacer"></span><span id="sx-state" class="badge">állapot…</span></div>
      <p class="hint">Egy kattintással elindítja a hivatalos SearXNG-t Docker-konténerben (a JSON-kimenet és a helyi használathoz szükséges beállítások automatikusak),
        majd a címét beírja a SearXNG URL mezőbe, és keresőmotornak választja. Ha már fut egy példány, a „Keresés” gomb megtalálja és átveszi.</p>
      <div class="row">
        <label class="field" style="max-width:120px"><span>Port</span><input type="number" min="1024" max="65535" data-setting="web_searxng_port" value="${s.web_searxng_port}"></label>
        <label class="field" style="max-width:280px;min-width:230px"><span>Futtatás módja</span>${sel("web_searxng_mode", [["auto", "Automatikus (Docker)"], ["docker", "Docker"], ["native", "Python (searx csomag)"]])}</label>
        <label class="check" style="margin-top:14px"><input type="checkbox" data-setting="web_searxng_autostart" ${s.web_searxng_autostart ? "checked" : ""}> Induljon az LLM Arénával együtt</label>
        <span class="spacer"></span>
        <button class="btn primary" id="sx-start" style="margin-top:12px">▶ SearXNG indítása</button>
        <button class="btn" id="sx-detect" style="margin-top:12px">🔍 Keresés helyi SearXNG után</button>
        <button class="btn danger" id="sx-stop" style="margin-top:12px">■ Leállítás</button>
      </div>
      <div id="sx-info" class="web-results"></div>
    </div>
    <div class="grid3">
      <label class="field"><span>Találat / domain (ismétlődés-szűrés)</span><input type="number" min="1" max="5" data-setting="web_per_domain" value="${s.web_per_domain}"></label>
      <label class="field"><span>Valódi böngésző (Playwright + stealth)</span>${sel("web_browser", [["fallback", "Tartalék – ha az egyszerű letöltés blokkolt (ajánlott)"], ["always", "Mindig böngészővel olvas"], ["off", "Kikapcsolva"]])}</label>
      <label class="field"><span>Böngésző</span>${sel("web_browser_channel", [["auto", "Automatikus (Chromium → Edge → Chrome)"], ["chromium", "Playwright Chromium"], ["msedge", "Microsoft Edge (telepített)"], ["chrome", "Google Chrome (telepített)"]])}</label>
    </div>
    <div class="row">
      <label class="field"><span>Böngésző elérési útja (opcionális)</span><input type="text" data-setting="web_browser_path" value="${esc(s.web_browser_path)}" placeholder="pl. C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"></label>
      <label class="check" style="margin-top:14px"><input type="checkbox" data-setting="web_browser_headless" ${s.web_browser_headless ? "checked" : ""}> Láthatatlan (headless)</label>
      <button class="btn" id="browser-test" style="margin-top:12px">🧭 Böngésző tesztelése</button>
      <button class="btn ghost" id="web-cache" style="margin-top:12px" title="Keresési és oldal-gyorsítótár ürítése">Gyorsítótár ürítése</button>
    </div>
    <div class="web-results" id="browser-results"></div>
    <label class="check"><input type="checkbox" data-setting="web_allow_private" ${s.web_allow_private ? "checked" : ""}> Helyi hálózati címek (localhost, LAN) lekérésének engedélyezése</label>
    <div class="row mt"><input type="text" id="web-q" placeholder="Próba-keresés, pl. llama.cpp latest release" style="flex:1">
      <button class="btn" id="web-test">🔎 Keresés tesztelése</button></div>
    <div class="web-results" id="web-results"></div>
    <p class="hint">Natív tool-híváshoz a llama-servert <code>--jinja</code> kapcsolóval indítsd. Ha a szerver nem támogatja, a program automatikusan szöveges protokollra vált.<br>
      Böngésző telepítése: <code>pip install playwright playwright-stealth</code>, majd <code>python -m playwright install chromium</code> – vagy válaszd a gépen lévő Edge/Chrome böngészőt.</p>
  </div>`;
}

async function loadSearxStatus() {
  const badge = root.querySelector("#sx-state");
  const info = root.querySelector("#sx-info");
  if (!badge) return;
  try {
    const st = (await api.searxngStatus()).status;
    const p = st.probe;
    if (p && p.searxng && p.json) {
      badge.className = "badge ok"; badge.textContent = "fut";
      info.innerHTML = `<div class="hint">✅ ${esc(p.instance_name || "SearXNG")} elérhető: <span class="mono">${esc(p.url)}</span>${st.container ? ` · konténer: ${esc(st.container)}` : ""}</div>`;
    } else if (p && p.searxng) {
      badge.className = "badge medium"; badge.textContent = "JSON tiltva";
      info.innerHTML = errorBox({ label: "A SearXNG fut, de nem ad JSON-t", message: p.error,
        hint: "A settings.yml-ben: search.formats: [html, json]. A „SearXNG indítása” gombbal indított példánynál ez automatikus." });
    } else {
      badge.className = "badge"; badge.textContent = st.container ? `konténer: ${st.container}` : "nem fut";
      const d = st.docker || {};
      info.innerHTML = d.ok ? `<div class="hint">Docker ${esc(d.version)} elérhető – indítható.</div>`
        : `<div class="hint">⚠ ${esc(d.message || "")} ${esc(d.hint || "")}${st.native_available ? " (A Python mód elérhető.)" : ""}</div>`;
    }
  } catch (e) { info.innerHTML = errorBox(e, "SearXNG állapot"); }
}

function collect() {
  const llms = { A: {}, B: {} };
  root.querySelectorAll("[data-slot][data-key]").forEach((el) => {
    const v = el.type === "checkbox" ? el.checked : el.value;
    llms[el.dataset.slot][el.dataset.key] = v;
  });
  const settings = {};
  root.querySelectorAll("[data-setting]").forEach((el) => {
    settings[el.dataset.setting] = el.type === "checkbox" ? el.checked : el.value;
  });
  return { llms, settings };
}

async function save(silent = true) {
  clearTimeout(saveTimer);
  try {
    await api.saveConfig(collect());
    if (!silent) toast("Beállítások mentve", "ok");
    refresh(50);
  } catch (e) { showError(e, "Beállítások mentése"); }
}

function renderSteps(slot, res) {
  const names = { health: "Elérhetőség (/health)", models: "Modellek (/v1/models)", completion: "Próba-válasz (chat completion)" };
  const badge = root.querySelector(`#conn-badge-${slot}`);
  badge.textContent = res.ok ? `OK · ${res.model || "?"}` : "HIBA";
  badge.className = `badge ${res.ok ? "ok" : "err"}`;
  root.querySelector(`#conn-${slot}`).innerHTML = res.steps.map((s) => s.error
    ? `<div>❌ <b>${names[s.step] || s.step}</b></div>${errorBox(s.error)}`
    : `<div>${s.ok ? "✅" : "❌"} <b>${names[s.step] || s.step}</b>: ${esc(s.info || "")}</div>`).join("") +
    `<div class="hint">Összesen ${res.latency}s</div>`;
  state.conn[slot] = { ok: res.ok, model: res.model, error: res.ok ? null : "kapcsolati hiba" };
}

function applySearxFields(url) {
  const port = (url.match(/:(\d+)\/?$/) || [])[1];
  const pf = root.querySelector('[data-setting="web_searxng_port"]');
  if (pf && port) { pf.value = port; state.project.settings.web_searxng_port = +port; }
  const u = root.querySelector('[data-setting="web_searxng_url"]');
  const b = root.querySelector('[data-setting="web_backend"]');
  if (u) u.value = url;
  if (b) b.value = "searxng";
  state.project.settings.web_searxng_url = url;
  state.project.settings.web_backend = "searxng";
}

export default {
  mount(el) {
    root = el;
    root.innerHTML = `<h1>LLM beállítások</h1>
      <p class="subtitle">Két független, OpenAI-kompatibilis endpoint (llama.cpp <code>llama-server</code>, vLLM, LM Studio, Ollama /v1…). A változások automatikusan mentődnek.</p>
      <div class="grid2">${llmCard("A")}${llmCard("B")}</div>${globalCard()}${webCard()}`;
    loadSearxStatus();
    root.addEventListener("input", (e) => {
      if (e.target.dataset.key === "temperature") root.querySelector(`#temp-${e.target.dataset.slot}`).textContent = e.target.value;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => save(true), 700);
    });
    root.addEventListener("change", (e) => { if (e.target.type === "checkbox" || e.target.tagName === "SELECT") save(true); });
    root.addEventListener("click", async (e) => {
      if (e.target.id === "sx-start") {
        try {
          await save(true);
          trackJob((await api.searxngStart()).job);
          root.querySelector("#sx-info").innerHTML = '<div class="hint">Indítás… (első alkalommal az image letöltése néhány percig tarthat – a folyamat a fejlécben látszik)</div>';
        } catch (err) { showError(err, "SearXNG indítása"); }
        return;
      }
      if (e.target.id === "sx-stop") {
        try { const r = await api.searxngStop(); toast(r.stopped ? "SearXNG leállítva" : "Nem futott kezelt SearXNG", "ok"); loadSearxStatus(); }
        catch (err) { showError(err, "SearXNG leállítása"); }
        return;
      }
      if (e.target.id === "sx-detect") {
        e.target.disabled = true;
        try {
          await save(true);
          const r = await api.searxngDetect();
          if (r.applied) {
            applySearxFields(r.applied);
            toast(`SearXNG megtalálva és beállítva: ${r.applied}`, "ok", 5000);
          } else if (r.found.length) {
            showError({ label: "Találtam SearXNG-t, de nem ad JSON-t", message: r.found.map((f) => `${f.url}: ${f.error}`).join("; "),
              hint: "A settings.yml-ben kapcsold be: search.formats: [html, json], majd indítsd újra." }, "SearXNG");
          } else {
            showError({ label: "Nem találtam futó SearXNG-t", message: "A helyi gépen a szokásos portokon (8888, 8080, 8081, …) nem válaszolt SearXNG.",
              hint: "Indítsd el a „▶ SearXNG indítása” gombbal (Docker Desktop szükséges), vagy írd be kézzel a címét a SearXNG URL mezőbe." }, "SearXNG");
          }
          loadSearxStatus();
        } catch (err) { showError(err, "SearXNG keresése"); } finally { e.target.disabled = false; }
        return;
      }
      if (e.target.id === "web-cache") {
        try { await api.webCacheClear(); toast("Webes gyorsítótár ürítve", "ok"); } catch (err) { showError(err, "Gyorsítótár"); }
        return;
      }
      if (e.target.id === "browser-test") {
        const out = root.querySelector("#browser-results");
        e.target.disabled = true;
        out.innerHTML = '<span class="hint">Böngésző indítása… (első alkalommal akár 10–20 s)</span>';
        try {
          await save(true);
          const r = (await api.browserTest()).result;
          if (!r.playwright) {
            out.innerHTML = errorBox({ label: "A Playwright nincs telepítve", message: "A valódi böngészős olvasáshoz telepíteni kell.",
              hint: "pip install playwright playwright-stealth  →  python -m playwright install chromium (vagy válaszd az Edge/Chrome böngészőt)." });
          } else if (!r.ok) {
            out.innerHTML = errorBox({ label: "A böngésző nem indult el", message: r.error || "ismeretlen hiba",
              hint: "Futtasd: python -m playwright install chromium, vagy állítsd a Böngésző mezőt Edge/Chrome-ra, esetleg add meg az elérési utat." });
          } else {
            out.innerHTML = `<div class="hint">✅ Böngésző: <b>${esc(r.browser)}</b> · stealth: ${r.stealth_active ? "aktív" : r.stealth ? "telepítve, de nem alkalmazva" : "<b>nincs telepítve</b> (pip install playwright-stealth)"} ·
              navigator.webdriver: ${esc(String(r.webdriver))}</div><div class="hint mono">${esc(r.ua || "")}</div>`;
          }
        } catch (err) { out.innerHTML = errorBox(err, "Böngészőteszt"); } finally { e.target.disabled = false; }
        return;
      }
      if (e.target.id === "web-test") {
        const out = root.querySelector("#web-results");
        e.target.disabled = true;
        out.innerHTML = '<span class="hint">Keresés…</span>';
        try {
          await save(true);
          const r = (await api.webTest(root.querySelector("#web-q").value)).result;
          out.innerHTML = r.ok
            ? `<div class="hint">✅ ${esc(r.summary)} (${r.duration}s)</div>`
              + ((r.attempts || []).length ? `<div class="hint">Tartalékra váltott: ${(r.attempts || []).map((a) => `${esc(a.backend)} – ${esc(a.error)}`).join("; ")}</div>` : "")
              + r.sources.map((s, i) => `<a href="${esc(/^https?:/i.test(s.url) ? s.url : "#")}" target="_blank" rel="noopener noreferrer">[${i + 1}] ${esc(s.title)}</a>`).join("")
            : errorBox({ kind: "web", label: "A keresés nem sikerült", message: r.summary, detail: r.error,
                hint: "Ellenőrizd az internetkapcsolatot / proxyt. Tartós blokkolásnál használj saját SearXNG-t vagy Brave API-kulcsot; a böngészős tartalékhoz telepítsd a Playwrightot." });
        } catch (err) { out.innerHTML = errorBox(err, "Webes keresés"); } finally { e.target.disabled = false; }
        return;
      }
      const t = e.target.closest("[data-test]");
      if (t) {
        const slot = t.dataset.test;
        t.disabled = true;
        root.querySelector(`#conn-${slot}`).innerHTML = '<div class="hint">Tesztelés…</div>';
        try {
          await save(true);
          const r = await api.testLLM(slot);
          renderSteps(slot, r.result);
          const modelInput = root.querySelector(`[data-slot="${slot}"][data-key="model"]`);
          if (r.result.models?.length) {
            root.querySelector(`#models-${slot}`).innerHTML = r.result.models.map((m) => `<option value="${esc(m)}">`).join("");
            if (!modelInput.value) modelInput.placeholder = `auto → ${r.result.models[0]}`;
          }
        } catch (err) { showError(err, "Beállítások"); } finally { t.disabled = false; }
        return;
      }
      const d = e.target.closest("[data-detect]");
      if (d) {
        const slot = d.dataset.detect;
        d.disabled = true;
        try {
          await save(true);
          const r = await api.detectModels(slot);
          if (!r.ok) { showError(r.error, `LLM ${slot} modellfelismerés`); return; }
          root.querySelector(`#models-${slot}`).innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
          const input = root.querySelector(`[data-slot="${slot}"][data-key="model"]`);
          input.placeholder = r.model ? `auto → ${r.model}` : "auto";
          toast(`LLM ${slot}: ${r.models.length} modell – ${r.models.join(", ") || "nincs lista"}`, "ok");
          state.conn[slot] = { ok: true, model: r.model };
        } catch (err) { showError(err, "Beállítások"); } finally { d.disabled = false; }
      }
    });
  },
  update(reason) {
    const sxBtn = root.querySelector("#sx-start");
    if (sxBtn) {
      const running = [...state.jobs.values()].some((j) => j.kind === "searxng" && j.status === "running");
      sxBtn.disabled = running;
      sxBtn.textContent = running ? "⏳ SearXNG indul…" : "▶ SearXNG indítása";
    }
    // After the SearXNG job: take over the new URL / backend into the (persistent) form.
    if (typeof reason === "string" && reason.startsWith("job_end:")) {
      const job = state.jobs.get(reason.slice(8));
      if (job?.kind === "searxng") {
        if (job.status === "done" && job.result?.url) applySearxFields(job.result.url);
        else api.project().then((d) => { if (d.project.settings.web_searxng_url) applySearxFields(d.project.settings.web_searxng_url); }).catch(() => {});
        loadSearxStatus();
      }
    }
  },
  unmount() { if (saveTimer) save(true); },
};
