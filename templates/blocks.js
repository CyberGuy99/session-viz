// Renders site.json content blocks (resolved by scripts/blocks.py). Inlined into both templates by build.py.
// SV_BLOCKS(blocks, position) → DocumentFragment
window.SV_BLOCKS = (() => {
  "use strict";
  const el = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    for (const k of kids.flat(Infinity)) if (k != null && k !== false && k !== "") e.append(k instanceof Node ? k : String(k));
    return e;
  };
  const plain = v => v == null ? "—" : typeof v === "number" ? (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2))
    : typeof v === "boolean" ? (v ? "yes" : "no") : String(v);
  const FORMATS = {
    duration: v => { const h = Math.floor(v / 3600), m = Math.round((v % 3600) / 60); return h ? `${h}h ${m}m` : `${m}m`; },
    percent: v => `${Math.round(v)}%`, usd: v => "$" + Number(v).toFixed(2), int: v => Math.round(v).toLocaleString(),
    date: v => new Date(v).toLocaleDateString(undefined, {dateStyle: "medium"}),
    datetime: v => new Date(v).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"}),
    short_id: v => String(v).slice(0, 8),
  };
  const fmt = (v, f) => v == null ? "—" : f && FORMATS[f] ? FORMATS[f](v) : plain(v);
  const label = s => String(s).replace(/_/g, " ");

  // minimal, escape-first markdown: headings, lists, paragraphs, **bold**, `code`, [text](http… or relative)
  function inline(text) {
    const frag = document.createDocumentFragment();
    const re = /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      frag.append(text.slice(last, m.index));
      if (m[1]) frag.append(el("strong", null, m[1]));
      else if (m[2]) frag.append(el("code", null, m[2]));
      else {
        const ok = /^(https?:\/\/|#|\.{0,2}\/|[\w-]+\.html)/.test(m[4]) && !/^javascript:/i.test(m[4]);
        if (ok) { const a = el("a", null, m[3]); a.href = m[4]; frag.append(a); } else frag.append(m[3]);
      }
      last = re.lastIndex;
    }
    frag.append(text.slice(last));
    return frag;
  }
  function markdown(src) {
    const root = el("div", "md");
    let list = null, para = [];
    const flush = () => { if (para.length) { root.append(el("p", null, inline(para.join(" ")))); para = []; } };
    for (const line of String(src || "").split("\n")) {
      const h = line.match(/^(#{1,4})\s+(.*)/), li = line.match(/^\s*[-*]\s+(.*)/);
      if (h) { flush(); list = null; root.append(el("h" + Math.min(6, h[1].length + 3), null, inline(h[2]))); }
      else if (li) { flush(); if (!list) root.append(list = el("ul")); list.append(el("li", null, inline(li[1]))); }
      else if (!line.trim()) { flush(); list = null; }
      else { list = null; para.push(line.trim()); }
    }
    flush();
    return root;
  }

  function body(b) {
    const F = b.format || {}, cols = b.columns || [];
    if (b.kind === "markdown") return markdown(b.text);
    if (b.kind === "stats") return el("div", "sv-stats", Object.entries(b.stats || {}).map(([k, v]) =>
      el("div", "sv-stat", el("div", "sv-stat-v", fmt(v, F[k])), el("div", "sv-stat-k", label(k)))));
    if (b.kind === "list" && b.items) return el("ul", null, b.items.map(i => el("li", null, inline(String(i)))));  // authored
    const rows = b.rows || [];
    if (!rows.length) return el("div", "sv-empty", "Nothing to show.");
    if (b.kind === "list") return el("ul", null, rows.map(r =>
      el("li", null, r.map((v, i) => v == null ? null : fmt(v, F[cols[i]])).filter(x => x != null).join(" · "))));
    const num = cols.map((_, i) => rows.every(r => r[i] == null || typeof r[i] === "number"));
    return el("div", "sv-scroll", el("table", "sv-table",
      el("thead", null, el("tr", null, cols.map((c, i) => el("th", num[i] ? "num" : null, label(c))))),
      el("tbody", null, rows.map(r => el("tr", null, r.map((v, i) =>
        el("td", num[i] ? "num" : null, fmt(v, F[cols[i]]))))))));
  }

  return (blocks, position) => {
    const frag = document.createDocumentFragment();
    for (const b of (blocks || []).filter(b => (b.position || "top") === position)) {
      frag.append(el("section", "sv-block",
        b.title && el("h3", "sv-block-title", b.title,
          b.as_of && el("span", "sv-asof", ` · as of ${b.as_of}`)),
        b.note && el("div", "sv-note", b.note),
        body(b)));
      frag.lastChild.dataset.block = b.id;
    }
    return frag;
  };
})();
