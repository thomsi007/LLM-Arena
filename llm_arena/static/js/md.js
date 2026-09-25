// Small, safe markdown renderer (escapes all HTML first; no external deps).

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function inline(s) {
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
}

export function markdown(src) {
  const text = esc(src || "").replace(/\r\n/g, "\n");
  const blocks = [];
  // Fenced code blocks first (also an unterminated trailing fence while streaming).
  let body = text.replace(/```([^\n`]*)\n([\s\S]*?)(```|$)/g, (_, info, code) => {
    blocks.push({ info: info.trim(), code: code.replace(/\n$/, "") });
    return `\n\u0001${blocks.length - 1}\u0001\n`;
  });
  const lines = body.split("\n");
  const out = [];
  let i = 0;
  const para = [];
  const flush = () => { if (para.length) { out.push(`<p>${inline(para.join("<br>"))}</p>`); para.length = 0; } };
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if ((m = line.match(/^\u0001(\d+)\u0001$/))) {
      flush();
      const b = blocks[+m[1]];
      out.push(`<pre><span class="lang">${b.info}</span><button class="btn small copy-code" data-copy-code>Másolás</button><code>${b.code}</code></pre>`);
      i++; continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flush();
      const lvl = Math.min(4, m[1].length);
      out.push(`<h${lvl}>${inline(m[2])}</h${lvl}>`);
      i++; continue;
    }
    if (/^\s*(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$/.test(line)) { flush(); out.push("<hr>"); i++; continue; }
    if (/^&gt;\s?/.test(line)) {
      flush();
      const q = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) q.push(lines[i++].replace(/^&gt;\s?/, ""));
      out.push(`<blockquote>${inline(q.join("<br>"))}</blockquote>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flush();
      const row = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = row(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(row(lines[i++]));
      out.push(`<table><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows
        .map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    if ((m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
      flush();
      const ordered = /\d/.test(m[2]);
      const items = [];
      while (i < lines.length && (m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
        const indent = m[1].length >= 2 ? ' style="margin-left:16px"' : "";
        let item = m[3].replace(/^\[( |x)\]\s/i, (_, x) => (x.trim() ? "☑ " : "☐ "));
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) item += " " + lines[i++].trim();
        items.push(`<li${indent}>${inline(item)}</li>`);
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }
    para.push(line);
    i++;
  }
  flush();
  return out.join("\n");
}
