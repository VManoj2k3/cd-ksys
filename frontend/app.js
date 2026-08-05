/* Cd koosys frontend — pure JS, no frameworks. */
"use strict";

const $ = (id) => document.getElementById(id);
const codeEl = $("code"), gutterEl = $("gutter"), resultsEl = $("results");
const layersEl = $("layers"), summaryEl = $("summary"), reviewBtn = $("review-btn");

const POLL_MS = 1000;
let pollTimer = null;

/* ---------------- editor gutter ---------------- */
function renderGutter(highlight = 0) {
  const n = codeEl.value.split("\n").length;
  let html = "";
  for (let i = 1; i <= n; i++) {
    html += i === highlight ? `<span class="hl">${i}</span>\n` : i + "\n";
  }
  gutterEl.innerHTML = html;
}
codeEl.addEventListener("input", () => renderGutter());
codeEl.addEventListener("scroll", () => { gutterEl.scrollTop = codeEl.scrollTop; });
renderGutter();

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
  renderGutter();
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
  try {
    const r = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        code,
        filename: $("filename").value || "snippet.py",
        language: $("language").value,   // dropdown is authoritative
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

function poll(jobId) {
  pollTimer = setTimeout(async () => {
    try {
      const r = await fetch(`/api/job/${jobId}`);
      if (!r.ok) throw new Error("job lost");
      const job = await r.json();
      renderLayers(job.layers);
      if (job.state === "done") { endReview(); renderResults(job); }
      else if (job.state === "error") {
        endReview();
        resultsEl.innerHTML = `<div class="banner">Review failed: ${esc(job.error)}</div>`;
      } else poll(jobId);
    } catch (err) {
      endReview();
      resultsEl.innerHTML = `<div class="banner">${esc(String(err.message || err))}</div>`;
    }
  }, POLL_MS);
}

/* ---------------- rendering ---------------- */
const LAYER_LABELS = {
  spell: "Spelling", lint: "Lint", security: "Security",
  hardcode: "Hardcoding", llm_review: "LLM review", fixes: "Fixes",
};

function renderLayers(layers) {
  layersEl.innerHTML = layers.map((l) => {
    const spin = l.state === "running" ? '<span class="spin"></span>' : "";
    const count = l.state === "done" && l.name !== "fixes" ? ` · ${l.found}` : "";
    const cls = l.state === "skipped" ? "" : l.state;
    return `<span class="layer-chip ${cls}" title="${esc(l.detail)}">${spin}${LAYER_LABELS[l.name] || l.name}${count}${l.state === "skipped" ? " · off" : ""}</span>`;
  }).join("");
}

function renderResults(job) {
  const vs = job.violations;
  const bySev = {};
  vs.forEach((v) => { bySev[v.severity] = (bySev[v.severity] || 0) + 1; });
  summaryEl.classList.remove("hidden");
  summaryEl.innerHTML =
    (job.language ? `<span>lang: <b>${esc(job.language)}</b></span>` : "") +
    `<span><b>${vs.length}</b> violation${vs.length === 1 ? "" : "s"}</span>` +
    ["critical", "high", "medium", "low", "info"]
      .filter((s) => bySev[s])
      .map((s) => `<span>${s}: <b>${bySev[s]}</b></span>`).join("") +
    (job.stats && job.stats.llm_raw_findings !== undefined
      ? `<span class="dim">LLM precision filter: ${job.stats.llm_raw_findings} raw → ${vs.filter((v) => v.layer === "llm").length} confirmed</span>`
      : "") +
    (vs.length ? `<button id="copy-btn" class="btn btn-copy">Copy all</button>` : "");

  if (!vs.length) {
    resultsEl.innerHTML = `<div class="empty-state"><p>No violations found.</p>${
      job.llm_available ? "" : '<p class="dim">Note: LLM layer was offline — only deterministic checks ran.</p>'}</div>`;
    return;
  }

  // group violations by their enclosing function (fall back to file scope)
  const groups = new Map();
  vs.forEach((v) => {
    const key = v.function || "(file scope)";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(v);
  });
  // order groups by the first violation's line
  const ordered = [...groups.entries()].sort(
    (a, b) => a[1][0].line - b[1][0].line);

  resultsEl.innerHTML =
    (job.notice ? `<div class="banner">${esc(job.notice)}</div>` : "") +
    (job.stats && job.stats.llm_capped
      ? `<div class="banner">${job.stats.llm_capped} lower-severity LLM finding(s) not shown (capped for performance on this file).</div>` : "") +
    (job.llm_available ? "" :
    '<div class="banner">llama-server offline — showing deterministic findings only; no LLM fixes generated.</div>') +
    ordered.map(([fn, items]) => {
      const label = fn === "(file scope)" ? "(file scope)" : `${esc(fn)}()`;
      return `<div class="fn-group">
        <div class="fn-header">${label} <span class="dim">${items.length} finding${items.length === 1 ? "" : "s"}</span></div>
        ${items.map(cardHTML).join("")}
      </div>`;
    }).join("");

  // wire up events
  resultsEl.querySelectorAll("[data-line]").forEach((el) =>
    el.addEventListener("click", () => jumpToLine(parseInt(el.dataset.line, 10))));
  resultsEl.querySelectorAll("[data-apply]").forEach((el) =>
    el.addEventListener("click", () => applyFix(job, el.dataset.apply)));
  const copyBtn = document.getElementById("copy-btn");
  if (copyBtn) copyBtn.addEventListener("click", () => copyViolations(job, copyBtn));
}

function copyViolations(job, btn) {
  const vs = job.violations;
  const groups = new Map();
  vs.forEach((v) => {
    const key = v.function || "(file scope)";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(v);
  });
  const ordered = [...groups.entries()].sort((a, b) => a[1][0].line - b[1][0].line);
  const lines = [`Code review — ${job.filename || ""} (${job.language || ""})`,
                 `${vs.length} violation(s)`, ""];
  ordered.forEach(([fn, items]) => {
    lines.push(fn === "(file scope)" ? "## (file scope)" : `## ${fn}()`);
    items.forEach((v) => {
      lines.push(`  L${v.line} [${v.layer}/${v.rule}] ${v.severity}: ${v.message}`);
      if (v.fix && v.fix.validated) {
        const at = v.fix.start_line && v.fix.start_line !== v.line
          ? ` (line ${v.fix.start_line})` : "";
        lines.push(`      fix${at}: ${v.fix.replacement.replace(/\n/g, " ")}`);
      } else if (v.suggestion || v.message) {
        lines.push(`      manual: ${v.suggestion || v.message}`);
      }
    });
    lines.push("");
  });
  const text = lines.join("\n");
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = "Copied ✓";
    setTimeout(() => { btn.textContent = "Copy all"; }, 2000);
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
    fixHTML = `<div class="nofix">No auto-fix available — manual change recommended: ${esc(v.suggestion || v.message)}</div>`;
  }
  const verif = v.layer === "llm"
    ? `<div class="verif ok">verified: ${esc(v.verification_note)}</div>`
    : (v.verification_note ? `<div class="verif ok">${esc(v.verification_note)}</div>` : "");
  return `<div class="card sev-${v.severity}">
    <div class="card-head">
      <span class="badge badge-${v.layer}">${v.layer}</span>
      <span class="rule">${esc(v.rule)}</span>
      <span class="line-link" data-line="${v.line}">line ${v.line}</span>
    </div>
    <div class="msg">${esc(v.message)}</div>
    <div class="snippet">${esc(v.snippet)}</div>
    ${verif}${fixHTML}
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
