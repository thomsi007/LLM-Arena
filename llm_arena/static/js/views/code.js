// Code view: files, versions, diffs, manual edit, download.
import { api } from "../api.js";
import { state, refresh } from "../state.js";
import { esc, toast, copyText, download, fmtDate, exportHtmlBtn } from "../ui.js";

let root;
let selected = null;
let version = null;     // null = current
let editing = false;
let showDiff = false;

const isTest = (n) => { const b = n.split("/").pop(); return b.startsWith("test_") || b.endsWith("_test.py"); };

function files() {
  const code = state.project.code;
  if (version) return (code.versions.find((v) => v.version === version) || {}).files || {};
  return code.files;
}

function diffHtml(diff) {
  if (!diff) return '<div class="hint">Nincs változás / első verzió.</div>';
  return `<pre class="diff">${diff.split("\n").map((l) => {
    const cls = l.startsWith("+++") || l.startsWith("---") ? "file" : l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
    return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l) + "\n";
  }).join("")}</pre>`;
}

function render() {
  const code = state.project.code;
  const f = files();
  const names = Object.keys(f).sort();
  if (!selected || !(selected in f)) selected = names.find((n) => !isTest(n) && n.endsWith(".py")) || names.find((n) => !isTest(n)) || names[0] || null;
  const group = (title, arr) => arr.length ? `<div class="grp">${title}</div>${arr.map((n) => `<a data-file="${esc(n)}" class="${n === selected ? "active" : ""}" title="${esc(n)}">${esc(n)}</a>`).join("")}` : "";
  const ver = version ? code.versions.find((v) => v.version === version) : code.versions[code.versions.length - 1];
  const content = selected ? f[selected] : "";
  const body = showDiff
    ? `<h3>Változások – v${ver?.version ?? "?"} (${esc(ver?.source || "")}) ${esc(ver?.note || "")}</h3>${diffHtml(ver?.diff)}`
    : editing
      ? `<textarea class="code-edit" id="code-edit" spellcheck="false">${esc(content)}</textarea>
         <div class="row mt"><input type="text" id="code-note" placeholder="Módosítás leírása (opcionális)"><button class="btn" id="code-cancel">Mégse</button><button class="btn primary" id="code-save">Mentés új verzióként</button></div>`
      : selected
        ? `<pre class="code">${content.split("\n").map((l) => `<span class="ln">${esc(l) || " "}</span>`).join("")}</pre>`
        : '<div class="empty">Még nincs generált kód. Indíts egy programtervezést vagy a teljes folyamatot.</div>';
  root.querySelector("#code-body").innerHTML = `
    <div class="row" style="margin-bottom:10px">
      <label class="field" style="max-width:360px"><span>Verzió</span><select id="code-version">
        <option value="">Aktuális (v${code.versions.length})</option>
        ${[...code.versions].reverse().map((v) => `<option value="${v.version}" ${v.version === version ? "selected" : ""}>v${v.version} · ${esc(v.source)} · ${fmtDate(v.created)}</option>`).join("")}
      </select></label>
      <span class="spacer"></span>
      <button class="btn" id="code-diff" ${code.versions.length ? "" : "disabled"}>${showDiff ? "Fájlnézet" : "± Változások"}</button>
      <button class="btn" id="code-copy" ${selected ? "" : "disabled"}>Másolás</button>
      <button class="btn" id="code-dl-file" ${selected ? "" : "disabled"}>⤓ Fájl</button>
      <a class="btn" href="/api/code/download${version ? "?version=" + version : ""}" ${names.length ? "" : 'style="pointer-events:none;opacity:.45"'}>⤓ ZIP</a>
      ${names.length ? exportHtmlBtn("code", "⤓ HTML") : ""}
      <button class="btn" id="code-new" ${version ? "disabled" : ""}>+ Új fájl</button>
      <button class="btn" id="code-edit-btn" ${selected && !version ? "" : "disabled"}>✎ Szerkesztés</button>
      <button class="btn danger" id="code-del" ${selected && !version ? "" : "disabled"}>Törlés</button>
    </div>
    <div class="codeview">
      <div class="filelist">${group("Kód", names.filter((n) => !isTest(n)))}${group("Tesztek", names.filter(isTest))}${names.length ? "" : '<div class="hint" style="padding:8px">nincs fájl</div>'}</div>
      <div>${selected && !showDiff ? `<div class="hint mono" style="margin-bottom:4px">${esc(selected)} · ${content.split("\n").length} sor</div>` : ""}${body}</div>
    </div>
    <h2>Módosítások</h2>
    <table class="tbl"><thead><tr><th>Idő</th><th>Típus</th><th>Leírás</th><th>Fájlok</th></tr></thead><tbody>
      ${[...state.project.changes].reverse().slice(0, 60).map((c) => `<tr><td>${fmtDate(c.ts)}</td><td>${esc(c.type)}</td><td>${esc(c.description)}</td><td class="mono">${esc((c.files || []).join(", "))}</td></tr>`).join("") || '<tr><td colspan="4" class="hint">nincs</td></tr>'}
    </tbody></table>`;
}

export default {
  mount(el) {
    root = el;
    editing = false;
    root.innerHTML = `<h1>Kódnézet</h1><p class="subtitle">A generált program és tesztjei, verziókezeléssel. Minden modell-javítás és kézi módosítás új verziót hoz létre.</p><div id="code-body"></div>`;
    root.addEventListener("click", async (e) => {
      const fa = e.target.closest("[data-file]");
      if (fa) { selected = fa.dataset.file; editing = false; showDiff = false; render(); return; }
      const id = e.target.id;
      const f = files();
      if (id === "code-copy") copyText(f[selected] || "");
      else if (id === "code-dl-file") download(selected.split("/").pop(), f[selected] || "", "text/x-python");
      else if (id === "code-diff") { showDiff = !showDiff; editing = false; render(); }
      else if (id === "code-edit-btn") { editing = true; showDiff = false; render(); }
      else if (id === "code-cancel") { editing = false; render(); }
      else if (id === "code-save") {
        const content = root.querySelector("#code-edit").value;
        try {
          const r = await api.editCode({ [selected]: content }, [], root.querySelector("#code-note").value);
          toast(`Mentve: v${r.version}`, "ok");
          editing = false;
          refresh(10);
        } catch (err) { toast(err.message, "error"); }
      } else if (id === "code-new") {
        const name = prompt("Új fájl neve (pl. utils.py vagy test_extra.py):");
        if (!name) return;
        try {
          await api.editCode({ [name]: "" }, [], `Új fájl: ${name}`);
          state.project.code.files[name] = "";
          selected = name; editing = true; showDiff = false; version = null;
          render(); refresh(10);
        } catch (err) { toast(err.message, "error"); }
      } else if (id === "code-del") {
        if (!confirm(`Törlöd: ${selected}?`)) return;
        try { await api.editCode({}, [selected], `Törölve: ${selected}`); selected = null; refresh(10); } catch (err) { toast(err.message, "error"); }
      }
    });
    root.addEventListener("change", (e) => {
      if (e.target.id === "code-version") { version = e.target.value ? +e.target.value : null; editing = false; render(); }
    });
  },
  update(reason) {
    if (reason === "jobs" || editing) return;
    render();
  },
};
