"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* auth header (mirrors app.js) */
(async () => {
  try {
    const me = await (await fetch("/api/me")).json();
    if (me.auth_mode && me.auth_mode !== "none" && me.user) {
      $("userbox").classList.remove("hidden");
      $("username-label").textContent = me.user;
    }
  } catch { /* ignore */ }
})();
const logoutBtn = $("logout-btn");
if (logoutBtn) logoutBtn.addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  window.location.href = "/login";
});

function msg(text, kind) {
  const m = $("coll-msg");
  m.textContent = text || "";
  m.className = "coll-msg" + (kind ? " " + kind : "");
}

async function load() {
  let data;
  try {
    const r = await fetch("/api/collections");
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (r.status === 404) {
      $("coll-list").innerHTML = '<div class="banner">Guideline collections are disabled on this server.</div>';
      return;
    }
    data = await r.json();
  } catch { $("coll-list").innerHTML = '<div class="banner">Could not load collections.</div>'; return; }

  const cols = data.collections || [];
  if (!cols.length) {
    $("coll-list").innerHTML = '<div class="empty-state"><p>No collections yet.</p><p class="dim">Create one above, then upload guideline PDFs into it.</p></div>';
    return;
  }
  $("coll-list").innerHTML = cols.map((c) => `
    <div class="coll-card" data-id="${esc(c.id)}">
      <div class="coll-head">
        <h3>${esc(c.name)}</h3>
        <button class="btn btn-danger" data-del="${esc(c.id)}">Delete</button>
      </div>
      <div class="coll-stats dim">${c.chunks} rules indexed · ${(c.docs || []).length} document(s)</div>
      <div class="coll-docs">${(c.docs || []).map((d) => `<span class="doc-chip">${esc(d)}</span>`).join("") || '<span class="dim">no documents yet</span>'}</div>
      <div class="coll-upload">
        <label class="btn btn-ghost" for="up-${esc(c.id)}">Upload PDF</label>
        <input id="up-${esc(c.id)}" type="file" accept=".pdf" hidden data-up="${esc(c.id)}">
        <span class="up-status dim" id="ups-${esc(c.id)}"></span>
      </div>
    </div>`).join("");

  document.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", () => del(b.dataset.del)));
  document.querySelectorAll("[data-up]").forEach((inp) =>
    inp.addEventListener("change", () => upload(inp.dataset.up, inp)));
}

async function create() {
  const name = $("new-name").value.trim();
  if (!name) { msg("Enter a name first.", "err"); return; }
  const btn = $("create-btn"); btn.disabled = true;
  try {
    const r = await fetch("/api/collections", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!r.ok) { msg((await r.json()).detail || "Create failed", "err"); return; }
    $("new-name").value = "";
    msg("Collection created.", "ok");
    load();
  } finally { btn.disabled = false; }
}

async function del(id) {
  if (!confirm("Delete this collection and all its indexed rules?")) return;
  const r = await fetch(`/api/collections/${id}`, { method: "DELETE" });
  if (r.ok) { msg("Collection deleted.", "ok"); load(); }
  else msg("Delete failed.", "err");
}

async function upload(id, input) {
  const f = input.files[0];
  if (!f) return;
  const st = $(`ups-${id}`);
  st.textContent = `Indexing ${f.name}…`;
  const fd = new FormData(); fd.append("file", f);
  try {
    const r = await fetch(`/api/collections/${id}/upload`, { method: "POST", body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { st.textContent = ""; msg(j.detail || "Upload failed", "err"); return; }
    st.textContent = `Indexed ${j.chunks} rules from ${esc(f.name)}.`;
    load();
  } catch { st.textContent = ""; msg("Upload failed — is the server reachable?", "err"); }
  finally { input.value = ""; }
}

$("create-btn").addEventListener("click", create);
$("new-name").addEventListener("keydown", (e) => { if (e.key === "Enter") create(); });
load();
