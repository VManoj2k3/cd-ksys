const f = document.getElementById("login-form");
f.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  const btn = f.querySelector("button");
  btn.disabled = true; btn.textContent = "Signing in…";
  try {
    const r = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
      }),
    });
    if (r.ok) { window.location.href = "/"; return; }
    const j = await r.json().catch(() => ({}));
    err.textContent = j.detail || "Invalid credentials";
  } catch { err.textContent = "Sign-in failed — is the server reachable?"; }
  btn.disabled = false; btn.textContent = "Sign in";
});
