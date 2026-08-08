/* Cd koosys frontend — pure JS, no frameworks. */
"use strict";

const $ = (id) => document.getElementById(id);
const codeEl = $("code"), gutterEl = $("gutter"), resultsEl = $("results");
const layersEl = $("layers"), summaryEl = $("summary"), reviewBtn = $("review-btn");
const filterbarEl = $("filterbar");

const POLL_MS = 1000;
let pollTimer = null;

const SEV_RANK = { critical: 5, high: 4, medium: 3, low: 2, info: 1 };
let currentJob = null;
let findingLines = new Map();                 // reviewed line -> {sev, title}
const filterState = { hidden: new Set(), fixableOnly: false, sort: "location" };

/* ---------------- editor gutter ---------------- */
function renderGutter(highlight = 0) {
  const n = codeEl.value.split("\n").length;
  let html = "";
  for (let i = 1; i <= n; i++) {
    if (i === highlight) { html += `<span class="hl">${i}</span>\n`; continue; }
    const f = findingLines.get(i);
    html += f
      ? `<span class="mark mark-${f.sev}" data-goto="${i}" title="${esc(f.title)}">${i}</span>\n`
      : i + "\n";
  }
  gutterEl.innerHTML = html;
}
codeEl.addEventListener("input", () => { if (findingLines.size) findingLines.clear(); renderGutter(); });
codeEl.addEventListener("scroll", () => { gutterEl.scrollTop = codeEl.scrollTop; });
gutterEl.addEventListener("click", (e) => {
  const el = e.target.closest("[data-goto]");
  if (el) scrollResultsToLine(parseInt(el.dataset.goto, 10));
});
renderGutter();

/* jump from a gutter marker to the matching finding card */
function scrollResultsToLine(line) {
  const card = resultsEl.querySelector(`.card[data-line="${line}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1100);
}

function jumpToLine(line) {
  const lh = parseFloat(getComputedStyle(codeEl).lineHeight);
  codeEl.scrollTop = Math.max(0, (line - 4) * lh);
  renderGutter(line);
  setTimeout(() => renderGutter(), 2500);
}

/* ---------------- language / upload ---------------- */
const EXT_TO_LANG = { py: "py", c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp",
  hpp: "cpp", hh: "cpp", java: "java", ts: "ts", tsx: "ts", js: "js", jsx: "js", mjs: "js" };

$("language").addEventListener("change", () => {
  const ext = $("language").value;
  const name = $("filename").value || "snippet";
  $("filename").value = name.replace(/\.[A-Za-z]+$/, "") + "." + ext;
});

$("file-input").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  codeEl.value = await f.text();
  $("filename").value = f.name;
  const ext = (f.name.split(".").pop() || "").toLowerCase();
  if (EXT_TO_LANG[ext]) $("language").value = EXT_TO_LANG[ext];
  findingLines.clear();
  renderGutter();
});

/* ---------------- "Try an example" ---------------- */
const EXAMPLES = {
  py: 'import hashlib, subprocess\n\n# Calcualte the md5 hash for the recieved data\ndef process(data, cache={}):\n    h = hashlib.md5(data).hexdigest()\n    if h == None:\n        return None\n    subprocess.call("echo " + h, shell=True)\n    return h\n',
  c: '#include <stdio.h>\n#include <string.h>\n\nint main(void) {\n  char buf[16];\n  gets(buf);            /* unbounded read */\n  char dst[8];\n  strcpy(dst, buf);     /* no bounds check */\n  return 0;\n}\n',
  cpp: '#include <cstddef>\nusing namespace std;\n\nint* make() {\n  int* p = new int(5);\n  if (p == NULL) { return NULL; }\n  return p;\n}\n',
  java: 'public class Svc {\n  String check(String a, String b) {\n    System.out.println("checking");\n    if (a == b) { return "same"; }\n    try { return a.trim(); } catch (Exception e) { return ""; }\n  }\n}\n',
  ts: 'function parse(input: any): number {\n  console.log(input);\n  if (input == null) { return 0; }\n  return input.length;\n}\n',
  js: 'function parse(input) {\n  console.log(input);\n  if (input == null) { return 0; }\n  return eval(input);\n}\n',
};
$("example-btn").addEventListener("click", () => {
  const lang = $("language").value;
  codeEl.value = EXAMPLES[lang] || EXAMPLES.py;
  $("filename").value = "example." + lang;
  findingLines.clear();
  renderGutter();
});

/* ---------------- keyboard shortcuts ---------------- */
document.addEventListener("keydown", (e) => {
  // Cmd/Ctrl+Enter runs a review from anywhere (incl. the editor)
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    if (!reviewBtn.disabled) reviewBtn.click();
    return;
  }
  // j / k step through findings — but not while typing in a field
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "textarea" || tag === "input" || tag === "select") return;
  if (e.key !== "j" && e.key !== "k") return;
  const cards = [...resultsEl.querySelectorAll(".card")];
  if (!cards.length) return;
  e.preventDefault();
  let idx = cards.findIndex((c) => c.classList.contains("active"));
  cards.forEach((c) => c.classList.remove("active"));
  if (idx < 0) idx = e.key === "j" ? 0 : cards.length - 1;
  else idx = e.key === "j" ? Math.min(cards.length - 1, idx + 1) : Math.max(0, idx - 1);
  const card = cards[idx];
  card.classList.add("active");
  card.scrollIntoView({ behavior: "smooth", block: "center" });
});

// close the export dropdown when clicking anywhere else
document.addEventListener("click", () => {
  const m = document.getElementById("export-menu");
  if (m && !m.classList.contains("hidden")) m.classList.add("hidden");
});

/* ---------------- health pill ---------------- */
async function refreshHealth() {
  const pill = $("llm-pill");
  try {
    const h = await (await fetch("/api/health")).json();
    if (h.llm_available) { pill.textContent = "LLM: online"; pill.className = "pill pill-ok"; }
    else { pill.textContent = "LLM: offline (deterministic only)"; pill.className = "pill pill-off"; }
  } catch { pill.textContent = "backend unreachable"; pill.className = "pill pill-off"; }
}
refreshHealth();
setInterval(refreshHealth, 15000);

/* ---------------- RAG: guideline collections ---------------- */
const ragToggle = $("rag-toggle"), ragBar = $("rag-bar");
const ragCollections = $("rag-collections"), ragHint = $("rag-hint");
function collectionCount() {
  return ragCollections ? ragCollections.querySelectorAll("input[type=checkbox]").length : 0;
}
function selectedCollections() {
  if (!ragToggle || !ragToggle.checked || !ragCollections) return [];
  return Array.from(ragCollections.querySelectorAll("input:checked")).map((i) => i.value);
}
async function initRag() {
  try {
    const h = await (await fetch("/api/health")).json();
    if (!h.rag_enabled) return;                 // Phase 2 disabled in config
    ragBar.classList.remove("hidden");
    const { collections } = await (await fetch("/api/collections")).json();
    if (!collections || !collections.length) { ragHint.classList.remove("hidden"); return; }
    // one checkbox chip per collection — check several to review against them
    // all at once (all selected by default). Clearer than a ctrl-click select.
    ragCollections.innerHTML = collections.map((c) =>
      `<label class="coll-check"><input type="checkbox" value="${esc(c.id)}" checked>
        ${esc(c.name)} <span class="dim">${c.chunks}</span></label>`).join("");
  } catch { /* RAG optional — never block the review page */ }
}
if (ragToggle) {
  ragToggle.addEventListener("change", () => {
    const on = ragToggle.checked && collectionCount() > 0;
    ragCollections.classList.toggle("hidden", !on);
    if (ragToggle.checked && collectionCount() === 0) ragHint.classList.remove("hidden");
  });
  initRag();
}

/* ---------------- auth: show user, wire logout, handle 401 ---------------- */
async function refreshUser() {
  try {
    const me = await (await fetch("/api/me")).json();
    if (me.auth_mode && me.auth_mode !== "none" && me.user) {
      $("userbox").classList.remove("hidden");
      $("username-label").textContent = me.user;
    }
  } catch { /* ignore */ }
}
refreshUser();
const logoutBtn = document.getElementById("logout-btn");
if (logoutBtn) logoutBtn.addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  window.location.href = "/login";
});
function handle401(r) {
  if (r.status === 401) { window.location.href = "/login"; return true; }
  return false;
}

/* ---------------- review flow ---------------- */
reviewBtn.addEventListener("click", async () => {
  const code = codeEl.value;
  if (!code.trim()) return;
  reviewBtn.disabled = true;
  reviewBtn.textContent = "Reviewing…";
  resultsEl.innerHTML = "";
  summaryEl.classList.add("hidden");
  layersEl.classList.remove("hidden");
  layersEl.innerHTML = "";
  pollFailures = 0;
  renderedProgressive = false;
  try {
    const r = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        code,
        filename: $("filename").value || "snippet.py",
        language: $("language").value,   // dropdown is authoritative
        rag_enabled: ragToggle ? ragToggle.checked : false,
        collection_ids: selectedCollections(),
      }),
    });
    if (handle401(r)) return;
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const { job_id } = await r.json();
    poll(job_id);
  } catch (err) {
    endReview();
    resultsEl.innerHTML = `<div class="banner">Request failed: ${esc(String(err.message || err))}</div>`;
  }
});

function endReview() {
  reviewBtn.disabled = false;
  reviewBtn.textContent = "Review code";
  if (pollTimer) clearTimeout(pollTimer);
}

let pollFailures = 0;
const MAX_POLL_FAILURES = 15;   // tolerate transient tunnel blips on long reviews
let renderedProgressive = false;

function poll(jobId) {
  pollTimer = setTimeout(async () => {
    try {
      const r = await fetch(`/api/job/${jobId}`);
      if (r.status === 404) throw new Error("job expired");
      if (!r.ok) { pollFailures++; if (pollFailures <= MAX_POLL_FAILURES) return poll(jobId); throw new Error("server not responding"); }
      const job = await r.json();
      pollFailures = 0;                 // reset on any good poll
      renderLayers(job.layers);
      // progressive: show findings as soon as they're attached (fixes may
      // still be generating) so a long fix stage doesn't hide the results
      if (!renderedProgressive && job.violations && job.violations.length) {
        renderedProgressive = true;
        renderResults(job);
      }
      if (job.state === "done") { endReview(); renderResults(job); }
      else if (job.state === "error") {
        endReview();
        // if we already showed findings, keep them and just note the error
        if (renderedProgressive) {
          const b = document.createElement("div");
          b.className = "banner"; b.textContent = "Note: " + job.error;
          resultsEl.prepend(b);
        } else {
          resultsEl.innerHTML = `<div class="banner">Review failed: ${esc(job.error)}</div>`;
        }
      } else poll(jobId);
    } catch (err) {
      pollFailures++;
      if (pollFailures <= MAX_POLL_FAILURES) return poll(jobId);   // keep trying
      endReview();
      resultsEl.innerHTML = `<div class="banner">Lost connection to the server after several retries: ${esc(String(err.message || err))}. The review may still be running — try refreshing.</div>`;
    }
  }, POLL_MS);
}

/* ---------------- rendering ---------------- */
const LAYER_LABELS = {
  spell: "Spelling", lint: "Lint", security: "Security",
  hardcode: "Hardcoding", llm_review: "LLM review", fixes: "Fixes",
  guideline: "Guidelines",
};

function renderLayers(layers) {
  const parts = [];
  layers.forEach((l, i) => {
    if (i > 0) {
      const prev = layers[i - 1].state;
      const done = prev === "done" || prev === "skipped";
      parts.push(`<div class="conn ${done ? "filled" : ""}"></div>`);
    }
    const known = ["running", "done", "skipped", "error"];
    const cls = known.includes(l.state) ? l.state : "pending";
    const hasFind = l.state === "done" && l.name !== "fixes" && l.found > 0;
    let node;
    if (l.state === "running") node = '<span class="spin"></span>';
    else if (l.state === "done") node = "✓";
    else if (l.state === "skipped") node = "–";
    else if (l.state === "error") node = "!";
    else node = "•";
    let count = "";
    if (l.state === "done") count = `<span class="stage-count">${l.found}</span>`;
    else if (l.state === "skipped") count = `<span class="stage-count dim">off</span>`;
    parts.push(`<div class="stage ${cls}${hasFind ? " has-findings" : ""}" title="${esc(l.detail || "")}">
      <div class="node">${node}</div>
      <span class="stage-name">${LAYER_LABELS[l.name] || l.name}</span>${count}
    </div>`);
  });
  layersEl.innerHTML = parts.join("");
}

function renderResults(job) {
  currentJob = job;
  const vs = job.violations;
  const bySev = {};
  vs.forEach((v) => { bySev[v.severity] = (bySev[v.severity] || 0) + 1; });
  const order = ["critical", "high", "medium", "low", "info"];
  const present = order.filter((s) => bySev[s]);
  const layersRun = (job.layers || []).filter((l) => l.state === "done").length;
  const llmStat = (job.stats && job.stats.llm_raw_findings !== undefined)
    ? `<span>LLM precision filter: <b>${job.stats.llm_raw_findings}</b> raw → <b>${vs.filter((v) => v.layer === "llm").length}</b> confirmed</span>`
    : "";
  const langMeta = job.language ? `<span>language <b>${esc(job.language)}</b></span>` : "";
  const suppMeta = (job.stats && job.stats.suppressed)
    ? `<span>🚫 <b>${job.stats.suppressed}</b> suppressed</span>` : "";
  const metaParts = [langMeta, llmStat, suppMeta].filter(Boolean);
  const metaHTML = metaParts.length
    ? `<div class="score-meta">${metaParts.join('<span class="sep">•</span>')}</div>` : "";
  summaryEl.classList.remove("hidden");

  // editor gutter markers: worst severity per line + a hover summary
  findingLines.clear();
  const byLine = new Map();
  vs.forEach((v) => { if (!byLine.has(v.line)) byLine.set(v.line, []); byLine.get(v.line).push(v); });
  byLine.forEach((items, ln) => {
    let top = items[0];
    items.forEach((v) => { if (SEV_RANK[v.severity] > SEV_RANK[top.severity]) top = v; });
    findingLines.set(ln, { sev: top.severity,
      title: items.map((v) => `${v.severity}: ${v.message}`).join("\n") });
  });
  renderGutter();

  if (!vs.length) {
    filterbarEl.classList.add("hidden");
    summaryEl.innerHTML =
      `<div class="score-main"><div class="score-num clean">✓</div>
        <div class="score-label"><b>No issues found</b><br>${layersRun} layer${layersRun === 1 ? "" : "s"} ran clean</div></div>` +
      metaHTML;
    resultsEl.innerHTML = `<div class="empty-state"><div class="clean-badge">✅</div>
      <p class="big">No violations found.</p>
      <p class="dim">${job.llm_available
        ? "Clean across every layer that ran — deterministic checks and LLM semantic review."
        : "Note: the LLM layer was offline — only the deterministic checks ran."}</p></div>`;
    return;
  }

  const segs = present.map((s) =>
    `<div class="sev-seg ${s}" style="flex:${bySev[s]}" title="${s}: ${bySev[s]}"></div>`).join("");
  const legend = present.map((s) =>
    `<span class="sev-chip ${s}">${s} <b>${bySev[s]}</b></span>`).join("");
  summaryEl.innerHTML =
    `<div class="score-main">
       <div class="score-num">${vs.length}</div>
       <div class="score-label"><b>issue${vs.length === 1 ? "" : "s"}</b> found<br>across ${layersRun} layer${layersRun === 1 ? "" : "s"}</div>
     </div>
     <div class="sev-bar">${segs}</div>
     <div class="sev-legend">${legend}</div>
     <button id="copy-btn" class="btn btn-copy">Copy report</button>
     <button id="export-btn" class="btn btn-export">Export ▾</button>
     <div id="export-menu" class="export-menu hidden">
       <button data-export="md">Markdown (.md)</button>
       <button data-export="sarif">SARIF (.sarif)</button>
     </div>` +
    metaHTML;
  const copyBtn = document.getElementById("copy-btn");
  if (copyBtn) copyBtn.addEventListener("click", () => copyViolations(job, copyBtn));
  const exportBtn = document.getElementById("export-btn");
  const exportMenu = document.getElementById("export-menu");
  if (exportBtn) exportBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    exportMenu.classList.toggle("hidden");
  });
  if (exportMenu) exportMenu.querySelectorAll("[data-export]").forEach((b) =>
    b.addEventListener("click", () => { exportReport(job, b.dataset.export); exportMenu.classList.add("hidden"); }));

  // fresh review resets any prior filter/sort state
  filterState.hidden.clear();
  filterState.fixableOnly = false;
  filterState.sort = "location";
  renderFilterBar(bySev, present);
  renderFindings();
}

function renderFilterBar(bySev, present) {
  filterbarEl.classList.remove("hidden");
  const fixable = currentJob.violations.filter((v) => v.fix && v.fix.validated).length;
  const chips = present.map((s) =>
    `<button class="fchip" data-sev="${s}" aria-pressed="true">${s} <b>${bySev[s]}</b></button>`).join("");
  filterbarEl.innerHTML =
    `<span class="filter-label">Filter</span>${chips}` +
    (fixable ? `<button class="fchip fix" data-fixable aria-pressed="false">🔧 fixable <b>${fixable}</b></button>` : "") +
    `<label class="fsort">Sort
       <select id="sort-sel">
         <option value="location">by location</option>
         <option value="severity">by severity</option>
       </select></label>
     <span class="filter-count" id="filter-count"></span>`;
  filterbarEl.querySelectorAll("[data-sev]").forEach((b) => b.addEventListener("click", () => {
    const s = b.dataset.sev;
    const nowHidden = !filterState.hidden.has(s);
    filterState.hidden[nowHidden ? "add" : "delete"](s);
    b.setAttribute("aria-pressed", String(!nowHidden));
    renderFindings();
  }));
  const fx = filterbarEl.querySelector("[data-fixable]");
  if (fx) fx.addEventListener("click", () => {
    filterState.fixableOnly = !filterState.fixableOnly;
    fx.setAttribute("aria-pressed", String(filterState.fixableOnly));
    renderFindings();
  });
  const ss = filterbarEl.querySelector("#sort-sel");
  if (ss) ss.addEventListener("change", () => { filterState.sort = ss.value; renderFindings(); });
}

function passesFilter(v) {
  if (filterState.hidden.has(v.severity)) return false;
  if (filterState.fixableOnly && !(v.fix && v.fix.validated)) return false;
  return true;
}

function renderFindings() {
  const job = currentJob;
  const vs = job.violations.filter(passesFilter);
  const banners =
    (job.notice ? `<div class="banner">${esc(job.notice)}</div>` : "") +
    (job.stats && job.stats.llm_capped
      ? `<div class="banner">${job.stats.llm_capped} lower-severity LLM finding(s) not shown (capped for performance on this file).</div>` : "") +
    (job.llm_available ? "" :
    '<div class="banner">llama-server offline — showing deterministic findings only; no LLM fixes generated.</div>');

  let body;
  if (!vs.length) {
    body = '<div class="empty-state"><p class="dim">No findings match the current filters.</p></div>';
  } else if (filterState.sort === "severity") {
    const sorted = [...vs].sort((a, b) =>
      (SEV_RANK[b.severity] - SEV_RANK[a.severity]) || (a.line - b.line));
    body = sorted.map(cardHTML).join("");
  } else {
    const groups = new Map();
    vs.forEach((v) => {
      const key = v.function || "(file scope)";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(v);
    });
    const ordered = [...groups.entries()].sort((a, b) => a[1][0].line - b[1][0].line);
    body = ordered.map(([fn, items]) => {
      const label = fn === "(file scope)" ? "(file scope)" : `${esc(fn)}()`;
      return `<div class="fn-group">
        <div class="fn-header">${label} <span class="dim">${items.length} finding${items.length === 1 ? "" : "s"}</span></div>
        ${items.map(cardHTML).join("")}
      </div>`;
    }).join("");
  }
  resultsEl.innerHTML = banners + body;

  const total = job.violations.length;
  const fc = document.getElementById("filter-count");
  if (fc) fc.textContent = vs.length === total ? `${total} shown` : `${vs.length} of ${total} shown`;

  resultsEl.querySelectorAll(".line-link[data-line]").forEach((el) =>
    el.addEventListener("click", () => jumpToLine(parseInt(el.dataset.line, 10))));
  resultsEl.querySelectorAll("[data-apply]").forEach((el) =>
    el.addEventListener("click", () => applyFix(job, el.dataset.apply)));
  resultsEl.querySelectorAll("[data-dismiss]").forEach((el) =>
    el.addEventListener("click", () => dismissFinding(job, el.dataset.dismiss)));
}

/* mark a finding as a false positive: drop a koosys:ignore marker on its line
   (so it stays suppressed on every future review) and re-run the review */
const COMMENT_TOKEN = { py: "#", c: "//", cpp: "//", java: "//", ts: "//", js: "//" };
function dismissFinding(job, vid) {
  const v = job.violations.find((x) => x.id === vid);
  if (!v) return;
  const cmt = COMMENT_TOKEN[$("language").value] || "#";
  const lines = codeEl.value.split("\n");
  const idx = v.line - 1;
  if (idx < 0 || idx >= lines.length) return;
  const existing = lines[idx].match(/koosys:ignore(\[([^\]]*)\])?/i);
  if (!existing) {
    lines[idx] = lines[idx].replace(/\s*$/, "") + `  ${cmt} koosys:ignore[${v.rule}]`;
  } else if (existing[1] !== undefined) {          // has a [rule,list] — add ours
    const rules = existing[2].split(",").map((s) => s.trim()).filter(Boolean);
    if (!rules.includes(v.rule)) {
      rules.push(v.rule);
      lines[idx] = lines[idx].replace(/koosys:ignore\[[^\]]*\]/i, `koosys:ignore[${rules.join(",")}]`);
    }
  }                                                // bare marker already covers it
  codeEl.value = lines.join("\n");
  findingLines.clear();
  renderGutter();
  reviewBtn.click();                               // re-review so counts/markers update
}

/* ---------------- report export (Markdown / SARIF) ---------------- */
function reportMarkdown(job) {
  const vs = job.violations;
  const bySev = {};
  vs.forEach((v) => { bySev[v.severity] = (bySev[v.severity] || 0) + 1; });
  const sevLine = ["critical", "high", "medium", "low", "info"]
    .filter((s) => bySev[s]).map((s) => `${s} ${bySev[s]}`).join(" · ");
  const out = [`# Code review — ${job.filename || "snippet"} (${job.language || ""})`, "",
    `**${vs.length} issue${vs.length === 1 ? "" : "s"}**${sevLine ? " — " + sevLine : ""} · engine: cd-koosys`, ""];
  const groups = new Map();
  vs.forEach((v) => { const k = v.function || "(file scope)"; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(v); });
  [...groups.entries()].sort((a, b) => a[1][0].line - b[1][0].line).forEach(([fn, items]) => {
    out.push(`## ${fn === "(file scope)" ? "(file scope)" : fn + "()"}`, "");
    items.forEach((v) => {
      out.push(`- **[${v.severity}]** \`${v.rule}\`${v.tool ? ` _(${v.tool})_` : ""} — L${v.line}: ${v.message}`);
      if (v.citation && v.citation.quote)
        out.push(`  - cites ${v.citation.source || "guideline"}${v.citation.page ? ` p${v.citation.page}` : ""}: "${v.citation.quote}"`);
      if (v.fix && v.fix.validated)
        out.push(`  - fix${v.fix.start_line && v.fix.start_line !== v.line ? ` (L${v.fix.start_line})` : ""}: \`${v.fix.replacement.replace(/\n/g, " ⏎ ")}\``);
      else if (v.suggestion) out.push(`  - manual: ${v.suggestion}`);
    });
    out.push("");
  });
  return out.join("\n");
}

function reportSarif(job) {
  const level = (s) => (s === "critical" || s === "high") ? "error" : (s === "medium" ? "warning" : "note");
  const rules = new Map();
  const results = job.violations.map((v) => {
    if (!rules.has(v.rule)) rules.set(v.rule,
      { id: v.rule, name: v.rule, shortDescription: { text: v.rule },
        properties: { layer: v.layer, tool: v.tool || "" } });
    const msg = v.message + (v.citation && v.citation.quote
      ? ` [cites ${v.citation.source || "guideline"}: ${v.citation.quote}]` : "");
    return {
      ruleId: v.rule, level: level(v.severity), message: { text: msg },
      locations: [{ physicalLocation: {
        artifactLocation: { uri: job.filename || "snippet" },
        region: { startLine: v.line, snippet: { text: v.snippet || "" } } } }],
      properties: { severity: v.severity, layer: v.layer, tool: v.tool || "",
        fix: (v.fix && v.fix.validated) ? v.fix.replacement : undefined },
    };
  });
  return JSON.stringify({
    version: "2.1.0",
    $schema: "https://json.schemastore.org/sarif-2.1.0.json",
    runs: [{ tool: { driver: {
      name: "cd-koosys", informationUri: "https://github.com/VManoj2k3/cd-ksys",
      version: "1.0", rules: [...rules.values()] } }, results }],
  }, null, 2);
}

function downloadFile(name, text, mime) {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function exportReport(job, kind) {
  const base = (job.filename || "review").replace(/\.[^.]*$/, "");
  if (kind === "sarif") downloadFile(base + ".sarif", reportSarif(job), "application/json");
  else downloadFile(base + ".koosys.md", reportMarkdown(job), "text/markdown");
}

function copyViolations(job, btn) {
  navigator.clipboard.writeText(reportMarkdown(job)).then(() => {
    btn.textContent = "Copied ✓";
    setTimeout(() => { btn.textContent = "Copy report"; }, 2000);
  }).catch(() => { btn.textContent = "Copy failed"; });
}

function cardHTML(v) {
  const fix = v.fix;
  let fixHTML = "";
  if (fix && fix.validated) {
    let diffRows;
    if (fix.file_wide && fix.replacement) {
      // replacement holds a precomputed unified diff (changed lines only)
      diffRows = fix.replacement.split("\n").map((ln) => {
        const cls = ln.startsWith("+") ? "add" : ln.startsWith("-") ? "del" : "ctx";
        return `<div class="${cls}">${esc(ln)}</div>`;
      }).join("");
    } else {
      // show the ACTUAL line(s) the fix changes, which may differ from the
      // violation's line (e.g. an uninitialized-var fix targets the
      // declaration, not the use-site). Pull the real "before" from the editor.
      const codeLines = codeEl.value.split("\n");
      const before = codeLines.slice(fix.start_line - 1, fix.end_line).join("\n");
      diffRows = `<div class="del">- ${esc(before || v.snippet)}</div><div class="add">+ ${esc(fix.replacement)}</div>`;
    }
    const atNote = (!fix.file_wide && fix.start_line && fix.start_line !== v.line)
      ? ` <span class="dim">— fix targets line ${fix.start_line}</span>` : "";
    fixHTML = `<div class="fixbox">
      <div class="fix-title">Validated fix <span class="dim">— ${esc(fix.validation_notes)}</span>${atNote}
        <button class="btn btn-apply" data-apply="${v.id}">Apply</button></div>
      <div class="diff">${diffRows}</div>
    </div>`;
  } else {
    fixHTML = `<div class="nofix">No auto-fix available — manual change recommended: ${esc(v.suggestion || v.message)}${v.fix_notes ? `<div class="dim">auto-fix attempts rejected: ${esc(v.fix_notes)}</div>` : ""}</div>`;
  }
  const verif = (v.layer === "llm" || v.layer === "guideline")
    ? `<div class="verif ok">verified: ${esc(v.verification_note)}</div>`
    : (v.verification_note ? `<div class="verif ok">${esc(v.verification_note)}</div>` : "");
  // guideline violations cite the exact uploaded rule they break
  let citeHTML = "";
  if (v.citation && v.citation.quote) {
    const c = v.citation;
    const src = [c.source, c.page ? `p${c.page}` : "", c.collection_name]
      .filter(Boolean).join(" · ");
    citeHTML = `<div class="citation"><div class="cite-src">📖 Guideline${src ? " — " + esc(src) : ""}</div>
      <div class="cite-quote">${esc(c.quote)}</div></div>`;
  }
  return `<div class="card sev-${v.severity}" data-line="${v.line}">
    <div class="card-head">
      <span class="sev-tag ${v.severity}">${esc(v.severity)}</span>
      <span class="badge badge-${v.layer}">${v.layer}</span>
      <span class="rule">${esc(v.rule)}</span>
      ${v.tool ? `<span class="tool-badge" title="Flagged by ${esc(v.tool)}">${esc(v.tool)}</span>` : ""}
      <span class="line-link" data-line="${v.line}">line ${v.line}</span>
      <button class="dismiss-btn" data-dismiss="${v.id}" title="Mark as false positive — inserts koosys:ignore on this line and re-reviews">✕ dismiss</button>
    </div>
    <div class="msg">${esc(v.message)}</div>
    <div class="snippet">${esc(v.snippet)}</div>
    ${citeHTML}${verif}${fixHTML}
  </div>`;
}

/* ---------------- apply fixes ---------------- */
function applyFix(job, vid) {
  const v = job.violations.find((x) => x.id === vid);
  if (!v || !v.fix || !v.fix.validated) return;
  const fix = v.fix;
  if (fix.file_wide && fix.fixed_code) {
    codeEl.value = fix.fixed_code;
  } else {
    const lines = codeEl.value.split("\n");
    const repl = fix.replacement === "" ? [] : fix.replacement.split("\n");
    lines.splice(fix.start_line - 1, fix.end_line - fix.start_line + 1, ...repl);
    codeEl.value = lines.join("\n");
  }
  renderGutter();
  jumpToLine(fix.start_line);
  const btn = resultsEl.querySelector(`[data-apply="${vid}"]`);
  if (btn) { btn.textContent = "Applied ✓"; btn.disabled = true; }
  // Other fixes reference the ORIGINAL line numbers, which just shifted.
  // Applying them now could patch the wrong lines — force a re-review instead.
  resultsEl.querySelectorAll("[data-apply]").forEach((b) => {
    if (b.dataset.apply !== vid && !b.disabled) {
      b.disabled = true;
      b.textContent = "Re-review to apply";
      b.title = "Line numbers changed after the last applied fix — run Review again.";
    }
  });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
