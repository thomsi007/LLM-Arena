// Self-test: checks every feature on this machine and shows exactly what fails.
import { api } from "../api.js";
import { esc, showError, copyText } from "../ui.js";

let root;
let last = null;

function render() {
  const out = root.querySelector("#diag-out");
  if (!last) { out.innerHTML = '<div class="empty">Nyomd meg az „Önellenőrzés indítása” gombot.</div>'; return; }
  const v = last.version;
  out.innerHTML = `
    <div class="stats">
      <div class="stat ok"><div class="v">${last.passed}</div><div class="k">Rendben</div></div>
      <div class="stat ${last.failed ? "err" : ""}"><div class="v">${last.failed}</div><div class="k">Hiba</div></div>
      <div class="stat"><div class="v mono" style="font-size:16px">${esc(v.commit || "?")}</div><div class="k">Verzió (commit) · ${esc(v.branch || "")}</div></div>
      <div class="stat"><div class="v" style="font-size:16px">Python ${esc(v.python)}</div><div class="k">${esc(v.os)}</div></div>
    </div>
    <table class="tbl"><thead><tr><th></th><th>Ellenőrzés</th><th>Eredmény</th><th>Idő</th></tr></thead><tbody>
    ${last.results.map((r) => `<tr><td>${r.ok ? "✅" : "❌"}</td><td><b>${esc(r.name)}</b></td>
      <td>${esc(r.detail || "")}${r.hint ? `<div class="err-hint">💡 ${esc(r.hint)}</div>` : ""}
        ${r.trace ? `<details class="err-details"><summary>Részletek</summary><pre>${esc(r.trace)}</pre></details>` : ""}</td>
      <td>${r.duration != null ? r.duration + " s" : ""}</td></tr>`).join("")}
    </tbody></table>
    <p class="hint">Hely: <span class="mono">${esc(v.path)}</span></p>`;
}

function asText() {
  const v = last.version;
  return [`LLM Aréna ${v.version} · commit ${v.commit} (${v.branch}) · Python ${v.python} · ${v.os}`, `Hely: ${v.path}`, ""]
    .concat(last.results.map((r) => `${r.ok ? "OK  " : "HIBA"}  ${r.name}: ${r.detail || ""}${r.hint ? `\n      tipp: ${r.hint}` : ""}${!r.ok && r.trace ? "\n" + r.trace : ""}`))
    .concat(["", `Összesen: ${last.passed} rendben, ${last.failed} hiba`]).join("\n");
}

export default {
  mount(el) {
    root = el;
    root.innerHTML = `<h1>Diagnosztika</h1>
      <p class="subtitle">Végigpróbálja a program minden funkcióját ezen a gépen (beépített próbamodellekkel), majd ellenőrzi a saját LLM-szervereidet, a webes keresést és a böngészőt.
        Ha valami nem működik, másold ki az eredményt, és küldd el – ebből pontosan látszik, mi a hiba.</p>
      <div class="card"><div class="row">
        <label class="check"><input type="checkbox" id="diag-quick"> Gyors mód (a programtervezés kihagyása)</label>
        <span class="spacer"></span>
        <button class="btn" id="diag-copy" disabled>Eredmény másolása</button>
        <button class="btn primary" id="diag-run">🩺 Önellenőrzés indítása</button></div></div>
      <div id="diag-out"></div>`;
    root.querySelector("#diag-run").onclick = async (e) => {
      e.target.disabled = true;
      root.querySelector("#diag-out").innerHTML = '<div class="card"><div class="hint">Ellenőrzés folyamatban… (kb. 10–60 másodperc)</div><div class="progress indeterminate"><div></div></div></div>';
      try {
        last = (await api.post("/api/selftest", { quick: root.querySelector("#diag-quick").checked })).report;
        root.querySelector("#diag-copy").disabled = false;
        render();
      } catch (err) { showError(err, "Diagnosztika"); root.querySelector("#diag-out").innerHTML = ""; } finally { e.target.disabled = false; }
    };
    root.querySelector("#diag-copy").onclick = () => last && copyText(asText());
    render();
  },
  update() {},
};
