/* Hangar front end. Plain JS, no build step. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  me: null,
  sessions: [],
  restorable: 0,
  overview: null,
  q: "",
  project: "",
  showArchived: false,
  view: null,          // current main view controller (has .dispose())
  sessionTab: {},      // remembered tab per session
};

/* ---------------- api ---------------- */

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Hangar": "1" }, credentials: "same-origin" };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  if (res.status === 401 && !["/api/login", "/api/setup", "/api/account/password", "/api/account/username"].includes(path)) {
    showLogin();
    throw new Error("Sign in required");
  }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
  return data;
}

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

/* Uploads one file as the raw request body (XHR for progress). Resolves to {path, name, size}. */
function uploadFile(file, dir, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/files/upload?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("X-Hangar", "1");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status === 401) showLogin();
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data?.detail || `Upload failed (HTTP ${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("Upload failed (network error)"));
    xhr.send(file);
  });
}

/* Uploads several files one after another, with progress in a toast. Returns the uploaded results. */
async function uploadAll(files, dir) {
  const list = [...files];
  const done = [];
  for (const [i, f] of list.entries()) {
    const label = list.length > 1 ? `(${i + 1}/${list.length}) ` : "";
    toast(`Uploading ${label}${f.name}…`, 600000);
    try {
      done.push(await uploadFile(f, dir, (p) => toast(`Uploading ${label}${f.name}… ${Math.round(p * 100)}%`, 600000)));
    } catch (e) {
      toast(`${f.name}: ${e.message}`, 6000);
      return done;
    }
  }
  toast(done.length === 1 ? `Uploaded ${done[0].name}` : `Uploaded ${done.length} files`);
  return done;
}

/* A path as Claude Code's @-mention, relative to the session folder when possible. */
function mention(path, base) {
  let rel = path;
  if (base && path.startsWith(base.replace(/\/$/, "") + "/")) rel = path.slice(base.replace(/\/$/, "").length + 1);
  return /\s/.test(rel) ? `@"${rel}"` : `@${rel}`;
}

/* ---------------- formatting ---------------- */

const fmt = {
  tokens(n) {
    n = n || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k";
    return String(Math.round(n));
  },
  pct(n) { return n == null ? "–" : Math.round(n) + "%"; },
  ago(ts) {
    if (!ts) return "";
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 86400 * 7) return Math.floor(s / 86400) + "d ago";
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  },
  until(iso) {
    if (!iso) return "";
    let s = (new Date(iso) - Date.now()) / 1000;
    if (s <= 0) return "now";
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    return `${m}m`;
  },
  when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const sameDay = d.toDateString() === new Date().toDateString();
    return sameDay ? d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
      : d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
  },
  money(n) { return n == null ? "" : "$" + n.toFixed(n >= 100 ? 0 : 2); },
  size(n) {
    if (n == null) return "";
    if (n < 1024) return n + " B";
    const u = ["KB", "MB", "GB", "TB"]; let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(n >= 10 ? 0 : 1) + " " + u[i];
  },
};

function level(p) {
  if (p >= 90) return { cls: "critical", label: "Near limit", icon: "●" };
  if (p >= 75) return { cls: "warning", label: "High", icon: "▲" };
  return { cls: "", label: "", icon: "" };
}

/* ---------------- auth ---------------- */

async function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  stopPolling();
  let st = { setup: false, username_hint: null };
  try { st = await (await fetch("/api/auth/state", { credentials: "same-origin" })).json(); } catch {}
  $("#setup-form").hidden = !st.setup;
  $("#login-form").hidden = st.setup;
  if (st.setup) {
    setTimeout(() => $("#setup-code").focus(), 50);
    return;
  }
  const sso = $("#oidc-login");
  sso.hidden = !st.oidc;
  if (st.oidc) sso.textContent = `Sign in with ${st.oidc.label}`;
  const pw = st.password !== false;
  $("#pw-fields").hidden = !pw;
  $("#login-or").hidden = !(st.oidc && pw);
  $("#login-submit").classList.toggle("primary", !st.oidc);
  for (const el of $("#pw-fields").querySelectorAll("input")) el.required = pw;
  const flash = takeQuery("oidc_error");
  if (flash) $("#login-error").textContent = flash;
  if (!pw) { setTimeout(() => sso.focus(), 50); return; }
  const user = $("#login-username");
  if (!user.value && st.username_hint) user.value = st.username_hint;
  setTimeout(() => (user.value ? $("#login-password") : user).focus(), 50);
}

/* Reads ?name=value left by a redirect (OIDC sign-in) and removes it from the address bar. */
function takeQuery(name) {
  const q = new URLSearchParams(location.search);
  const v = q.get(name);
  if (v === null) return null;
  q.delete(name);
  history.replaceState(null, "", location.pathname + (q.toString() ? "?" + q : "") + location.hash);
  return v;
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { username: $("#login-username").value, password: $("#login-password").value } });
    $("#login-password").value = "";
    boot();
  } catch (err) {
    $("#login-error").textContent = err.message;
    if (/set up/.test(err.message)) showLogin();
  }
});

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#setup-error");
  err.textContent = "";
  if ($("#setup-password").value !== $("#setup-confirm").value) {
    err.textContent = "The passwords don't match.";
    return;
  }
  try {
    await api("/api/setup", { method: "POST", body: { code: $("#setup-code").value, username: $("#setup-username").value,
      password: $("#setup-password").value } });
    for (const id of ["#setup-code", "#setup-password", "#setup-confirm"]) $(id).value = "";
    boot();
  } catch (ex) {
    err.textContent = ex.message;
    if (/already set up/.test(ex.message)) showLogin();
  }
});

/* ---------------- polling ---------------- */

const timers = {};
function startPolling() {
  stopPolling();
  const loopFn = (name, fn, ms) => {
    const tick = async () => {
      if (!document.hidden) { try { await fn(); } catch (e) { /* shown elsewhere */ } }
      timers[name] = setTimeout(tick, ms);
    };
    tick();
  };
  loopFn("sessions", loadSessions, 4000);
  loopFn("overview", loadOverview, 15000);
}
function stopPolling() { for (const k in timers) clearTimeout(timers[k]); }
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.me) { loadSessions(); loadOverview(); } });

async function loadSessions() {
  const data = await api("/api/sessions" + (state.q ? "?q=" + encodeURIComponent(state.q) : ""));
  state.sessions = data.sessions;
  state.restorable = data.restorable;
  renderSidebar();
  state.view?.onSessions?.();
}

async function loadOverview() {
  state.overview = await api("/api/overview");
  renderSideFoot();
  state.view?.onOverview?.();
}

/* ---------------- sidebar ---------------- */

function liveStatus(s) {
  const t = s.tmux;
  if (!t) return null;
  if (t.claude) return t.claude.status === "busy" ? "busy" : "idle";
  return "shell";
}
const STATUS_TEXT = { busy: "Working", idle: "Waiting", shell: "Shell" };
const agentName = (s) => (s.agent === "codex" ? "Codex" : "Claude");
/* Short tag for the sidebar: which agent, and whether it runs on an Ollama model. */
function agentTag(s) {
  const parts = [];
  if (s.agent === "codex") parts.push("Codex");
  if (s.ollama) parts.push("Ollama");
  return parts.join(" + ");
}

function sessionHref(s) {
  return s.id ? `#/s/${s.id}` : `#/t/${s.tmux?.name}`;
}

function renderSidebar() {
  const list = $("#session-list");
  const route = location.hash;
  const projects = [...new Set(state.sessions.map((s) => s.project).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const sel = $("#project-filter");
  const want = ["", ...projects].join("\n");
  if (sel.dataset.opts !== want) {
    sel.innerHTML = `<option value="">All projects</option>` + projects.map((p) => `<option>${esc(p)}</option>`).join("");
    sel.value = projects.includes(state.project) ? state.project : "";
    sel.dataset.opts = want;
  }

  const items = state.sessions.filter((s) =>
    (!state.project || s.project === state.project) && (state.showArchived ? s.meta?.archived : !s.meta?.archived || s.tmux));
  const running = items.filter((s) => s.tmux);
  const pinned = items.filter((s) => !s.tmux && s.meta?.pinned);
  const rest = items.filter((s) => !s.tmux && !s.meta?.pinned);

  const now = new Date();
  const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const groups = [["Today", startOfDay], ["Yesterday", startOfDay - 86400], ["Previous 7 days", startOfDay - 7 * 86400],
    ["Previous 30 days", startOfDay - 30 * 86400], ["Older", -Infinity]];
  const buckets = groups.map(() => []);
  for (const s of rest) buckets[groups.findIndex(([, t]) => (s.updated || 0) >= t)].push(s);

  const row = (s) => {
    const st = liveStatus(s);
    const href = sessionHref(s);
    const sub = [s.project, agentTag(s), st ? STATUS_TEXT[st] : fmt.ago(s.updated)].filter(Boolean).join(" · ");
    const dotCls = st || (s.meta?.pinned ? "pin" : "");
    return `<a class="s-item${route === href ? " active" : ""}" href="${esc(href)}" title="${esc(s.title)}">
      <span class="dot ${dotCls}" aria-hidden="true"></span><span class="s-title">${esc(s.title)}</span>
      <span class="s-sub">${esc(sub)}</span></a>`;
  };

  let html = "";
  if (running.length) html += `<div class="group-label">Running <span>${running.length}</span></div>` + running.map(row).join("");
  if (state.restorable && !running.length && !state.q) {
    html += `<div class="group-label">Last run</div><button class="btn small wide" data-act="restore">Restore ${state.restorable} session${state.restorable > 1 ? "s" : ""}</button>`;
  }
  if (pinned.length) html += `<div class="group-label">Pinned</div>` + pinned.map(row).join("");
  buckets.forEach((b, i) => { if (b.length) html += `<div class="group-label">${groups[i][0]}</div>` + b.map(row).join(""); });
  if (!items.length) html = `<div class="empty">${state.q ? "No sessions match." : state.showArchived ? "No archived sessions." : "No sessions yet. Start one with “New session”."}</div>`;
  list.innerHTML = html;
}

function renderSideFoot() {
  const o = state.overview;
  if (!o) return;
  const u = o.usage?.limits || [];
  const pick = ["five_hour", "seven_day"].map((k) => u.find((l) => l.key === k)).filter(Boolean);
  const meter = (label, p, title) => {
    const lv = level(p ?? 0);
    return `<div class="mini-meter" title="${esc(title || "")}"><span>${esc(label)}</span>
      <div class="bar"><i class="${lv.cls}" style="width:${Math.min(100, p || 0)}%"></i></div><span class="val">${fmt.pct(p)}</span></div>`;
  };
  const theme = document.documentElement.dataset.theme || "auto";
  $("#side-foot").innerHTML = `
    ${pick.map((l) => meter(l.key === "five_hour" ? "Session" : "Weekly", l.utilization, `${l.label}: resets in ${fmt.until(l.resets_at)}`)).join("")}
    ${o.error && !pick.length ? `<div class="dim" title="${esc(o.error)}">Usage unavailable</div>` : ""}
    <div class="foot-row"><span title="Host">${esc(state.me?.hostname || "")}</span>
      <span><a class="icon-btn" href="#/settings" title="Settings" aria-label="Settings">⚙</a>
      <button class="icon-btn" data-act="theme" title="Theme: ${theme}" aria-label="Change theme">${theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"}</button>
      <button class="icon-btn" data-act="logout" title="Sign out" aria-label="Sign out">⎋</button></span></div>`;
}

$("#search").addEventListener("input", (e) => {
  state.q = e.target.value.trim();
  clearTimeout(renderSidebar._t);
  renderSidebar._t = setTimeout(loadSessions, 200);
});
$("#project-filter").addEventListener("change", (e) => { state.project = e.target.value; renderSidebar(); });
$("#show-archived").addEventListener("change", (e) => { state.showArchived = e.target.checked; renderSidebar(); });
$("#new-session").addEventListener("click", () => openNewSession());
$("#close-side").addEventListener("click", () => toggleSide(false));
$("#scrim").addEventListener("click", () => toggleSide(false));
$("#session-list").addEventListener("click", (e) => {
  if (e.target.closest(".s-item")) toggleSide(false);
});

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  if (act === "logout") {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    showLogin();
  } else if (act === "theme") {
    const order = ["auto", "light", "dark"];
    const cur = document.documentElement.dataset.theme || "auto";
    const next = order[(order.indexOf(cur) + 1) % 3];
    if (next === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
    try { next === "auto" ? localStorage.removeItem("hangar.theme") : localStorage.setItem("hangar.theme", next); } catch {}
    renderSideFoot();
    state.view?.onTheme?.();
  } else if (act === "restore") {
    btn.disabled = true;
    const r = await api("/api/restore", { method: "POST" }).catch((err) => toast(err.message));
    if (r) toast(`Restored ${r.started.length} session${r.started.length === 1 ? "" : "s"}`);
    loadSessions();
  } else if (act === "dismiss-restore") {
    await api("/api/restore/dismiss", { method: "POST" }).catch(() => {});
    loadSessions();
  } else if (act === "menu") {
    toggleSide(true);
  } else if (act === "new") {
    openNewSession();
  }
});

function toggleSide(open) {
  $("#app").classList.toggle("side-open", open);
  $("#scrim").hidden = !open;
}

/* ---------------- charts ---------------- */

const tip = $("#tooltip");
function showTip(html, x, y) {
  tip.innerHTML = html;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let left = x + 14, top = y - r.height - 10;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  if (top < 8) top = y + 16;
  tip.style.left = left + "px";
  tip.style.top = top + "px";
}
function hideTip() { tip.hidden = true; }

/* Bar chart: one series, rounded data-end, 2px gaps, hover tooltip per bar. */
function barChart(values, { labels, tips, height = 120, highlightLast = false, ticks = 3, fmtY = fmt.tokens } = {}) {
  const W = 600, H = height, padL = 34, padB = 18, padT = 6;
  const max = Math.max(1, ...values);
  const n = values.length;
  const slot = (W - padL) / n;
  const bw = Math.max(2, slot - 2);
  let g = "";
  for (let i = 0; i <= ticks; i++) {
    const v = (max / ticks) * i;
    const y = H - padB - ((H - padB - padT) * i) / ticks;
    g += `<line class="gridline" x1="${padL}" x2="${W}" y1="${y}" y2="${y}"/><text class="axis" x="${padL - 6}" y="${y + 3.5}" text-anchor="end">${esc(fmtY(v))}</text>`;
  }
  values.forEach((v, i) => {
    const h = (v / max) * (H - padB - padT);
    const x = padL + i * slot + 1;
    const y = H - padB - h;
    const r = Math.min(4, bw / 2, h);
    // Rounded top, square base.
    const path = h <= 0 ? "" : `M${x},${H - padB} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + bw - r},${y} Q${x + bw},${y} ${x + bw},${y + r} L${x + bw},${H - padB} Z`;
    const dim = highlightLast && i !== n - 1 ? " dim" : "";
    g += `<path class="mark${dim}" d="${path}"/>`;
    g += `<rect class="hit" x="${padL + i * slot}" y="${padT}" width="${slot}" height="${H - padT}" data-tip="${esc(tips ? tips[i] : fmtY(v))}"/>`;
    if (labels && labels[i]) g += `<text class="axis" x="${x + bw / 2}" y="${H - 4}" text-anchor="middle">${esc(labels[i])}</text>`;
  });
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px" role="img">${g}</svg>`;
}

document.addEventListener("pointermove", (e) => {
  const hit = e.target.closest?.("[data-tip]");
  if (hit) { showTip(hit.dataset.tip, e.clientX, e.clientY); return; }
  hideTip();
});
document.addEventListener("pointerdown", (e) => { if (!e.target.closest?.("[data-tip]")) hideTip(); });

/* ---------------- router ---------------- */

function setMain(html) {
  $("#main").innerHTML = html;
}

function route() {
  state.view?.dispose?.();
  state.view = null;
  hideTip();
  const h = location.hash || "#/";
  let m;
  const plugin = pluginRoutes.find((r) => r.pattern.test(h));
  if (plugin) state.view = plugin.view(h.match(plugin.pattern));
  else if ((m = h.match(/^#\/s\/([\w-]+)/))) state.view = sessionView({ id: m[1] });
  else if ((m = h.match(/^#\/t\/([\w-]+)/))) state.view = sessionView({ tmuxName: m[1] });
  else if (h === "#/settings") state.view = settingsView();
  else if (h === "#/welcome") state.view = welcomeView();
  else state.view = dashboardView();
  renderSidebar();
  renderSideNav();
}
window.addEventListener("hashchange", route);

function topbar(title) {
  return `<div class="topbar"><button class="icon-btn" data-act="menu" aria-label="Open menu">☰</button>
    <span class="t">${esc(title)}</span><button class="icon-btn" data-act="new" aria-label="New session">+</button></div>`;
}

/* ---------------- AI providers (welcome page + settings) ---------------- */

/* Renders the Claude, ChatGPT, and Ollama cards into `root`. Returns { dispose }. */
function providerCards(root) {
  let disposed = false;
  let poll = null;
  const ui = { claudeCode: "", codexUrl: "", ollamaUrl: "", ollamaName: "", found: null, busy: {} };
  let data = null;

  const plan = (p) => (p ? ` · ${esc(p[0].toUpperCase() + p.slice(1))} plan` : "");
  const linkedLine = (who, p) => `<p class="linked"><span class="ok-mark" aria-hidden="true">✓</span> Linked${who ? ` as <b>${esc(who)}</b>` : ""}${plan(p)}</p>`;
  const errLine = (id) => `<p class="error" id="${id}" role="alert"></p>`;

  function claudeCard(c) {
    let body;
    if (c.linked) {
      body = `${linkedLine(c.email, c.plan)}
        <div class="form-actions"><button class="btn" data-pa="claude-logout">Unlink</button></div>`;
    } else if (c.login?.url) {
      body = `<ol class="steps">
          <li><span>Sign in on claude.ai.</span>
            <a class="btn primary" href="${esc(c.login.url)}" target="_blank" rel="noopener noreferrer">Open Claude sign-in ↗</a></li>
          <li><span>claude.ai then shows a code. Paste it here.</span>
            <form class="inline-form" data-pf="claude-code">
              <input type="text" id="claude-code" autocomplete="off" spellcheck="false" placeholder="Paste the code" value="${esc(ui.claudeCode)}" required>
              <button class="btn primary" type="submit">Finish</button></form></li>
        </ol>
        <div class="form-actions"><button class="btn ghost" data-pa="claude-cancel">Cancel</button></div>`;
    } else {
      body = `<p class="muted">Use your Claude subscription (Pro or Max). No API key needed.</p>
        <div class="form-actions"><button class="btn primary" data-pa="claude-login">Sign in with Claude</button></div>`;
    }
    return `<section class="card provider" aria-labelledby="pv-claude"><h3 id="pv-claude">Claude <small>Claude Code</small></h3>
      ${c.error ? `<p class="error">${esc(c.error)}</p>` : body}${errLine("claude-error")}
      <p class="hint">Login folder: <code>${esc(c.folder)}</code></p></section>`;
  }

  function codexCard(x) {
    let body;
    const inst = x.install || {};
    if (x.linked) {
      body = `${linkedLine(x.email, x.plan)}
        <div class="form-actions"><button class="btn" data-pa="codex-logout">Unlink</button></div>`;
    } else if (["downloading", "verifying", "unpacking"].includes(inst.state)) {
      const pct = inst.total_bytes ? Math.min(100, (100 * inst.done_bytes) / inst.total_bytes) : 0;
      const label = inst.state === "downloading" ? `Downloading Codex… ${Math.round(inst.done_bytes / 1e6)} of ${Math.round(inst.total_bytes / 1e6)} MB`
        : inst.state === "verifying" ? "Checking the download…" : "Unpacking…";
      body = `<p>${label}</p><div class="bar" role="progressbar" aria-valuenow="${Math.round(pct)}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct}%"></i></div>`;
    } else if (!x.installed) {
      body = `<p class="muted">Use your ChatGPT plan through OpenAI's Codex CLI. No API key needed.
          Hangar downloads Codex ${esc(x.version)} once (about ${x.download_mb} MB) and checks it before installing.</p>
        ${inst.state === "error" ? `<p class="error">${esc(inst.error)}</p>` : ""}
        <div class="form-actions"><button class="btn primary" data-pa="codex-install">Install Codex and sign in</button></div>`;
    } else if (x.login?.mode === "device" && x.login.code) {
      body = `<ol class="steps">
          <li><span>In ChatGPT, open Settings → Security and turn on <b>device code sign-in</b> for Codex. You only need to do this once.</span>
            <a class="btn" href="${esc(x.device_settings_url)}" target="_blank" rel="noopener noreferrer">Open ChatGPT settings ↗</a></li>
          <li><span>Open the Codex sign-in page and enter this code:</span>
            <div class="device-code"><code id="codex-code">${esc(x.login.code)}</code>
              <button class="btn small" data-pa="codex-copy" type="button">Copy</button></div>
            <a class="btn primary" href="${esc(x.login.url)}" target="_blank" rel="noopener noreferrer">Open ChatGPT sign-in ↗</a></li>
        </ol>
        <p class="waiting" aria-live="polite">${x.login.running ? "Waiting for you to finish signing in…" : esc(x.login.error || "Sign-in stopped.")}</p>
        <div class="form-actions"><button class="btn ghost" data-pa="codex-cancel">Cancel</button>
          <button class="btn ghost" data-pa="codex-browser">Use browser sign-in instead</button></div>`;
    } else if (x.login?.mode === "browser" && x.login.url) {
      body = `<ol class="steps">
          <li><span>Sign in with ChatGPT.</span>
            <a class="btn primary" href="${esc(x.login.url)}" target="_blank" rel="noopener noreferrer">Open ChatGPT sign-in ↗</a></li>
          <li><span>Afterwards your browser opens a page on <code>localhost:1455</code> that won't load. That's expected.
            Copy that page's whole address from the address bar and paste it here.</span>
            <form class="inline-form" data-pf="codex-callback">
              <input type="url" id="codex-url" autocomplete="off" spellcheck="false" placeholder="http://localhost:1455/auth/callback?code=…" value="${esc(ui.codexUrl)}" required>
              <button class="btn primary" type="submit">Finish</button></form></li>
        </ol>
        ${x.login.running ? "" : `<p class="error">${esc(x.login.error || "Sign-in stopped. Start again.")}</p>`}
        <div class="form-actions"><button class="btn ghost" data-pa="codex-cancel">Cancel</button></div>`;
    } else {
      body = `<p class="muted">Codex ${esc(x.version)} is installed. Sign in with your ChatGPT account.</p>
        <div class="form-actions"><button class="btn primary" data-pa="codex-login">Sign in with ChatGPT</button></div>`;
    }
    return `<section class="card provider" aria-labelledby="pv-codex"><h3 id="pv-codex">ChatGPT <small>Codex</small></h3>
      ${body}${errLine("codex-error")}</section>`;
  }

  function ollamaCard(o) {
    const rows = o.servers.map((sv) => `<li class="server">
        <div><b>${esc(sv.name)}</b> <code>${esc(sv.url)}</code><br>
          ${sv.ok ? `<span class="ok-text">✓ Ollama ${esc(sv.version || "")} · ${sv.models.length} model${sv.models.length === 1 ? "" : "s"}</span>`
            : `<span class="error">${esc(sv.error)}</span>`}</div>
        <button class="btn small ghost" data-pa="ollama-remove" data-id="${esc(sv.id)}" aria-label="Remove ${esc(sv.name)}">Remove</button></li>`).join("");
    const found = (ui.found || []).map((f) => `<li class="server found">
        <div>Found Ollama at <code>${esc(f.url)}</code><br><span class="muted">${f.models.length} model${f.models.length === 1 ? "" : "s"}</span></div>
        <button class="btn small" data-pa="ollama-add-found" data-url="${esc(f.url)}">Add</button></li>`).join("");
    return `<section class="card provider" aria-labelledby="pv-ollama"><h3 id="pv-ollama">Ollama <small>local models</small></h3>
      <p class="muted">Run open models on this machine or another one on your network. Claude Code and Codex sessions can use them.</p>
      ${rows || found ? `<ul class="servers">${rows}${found}</ul>` : ""}
      <form class="ollama-form" data-pf="ollama-add">
        <label class="field"><span>Address</span><input type="text" id="ollama-url" placeholder="http://192.168.1.20:11434" value="${esc(ui.ollamaUrl)}" required></label>
        <label class="field"><span>Name <small>(optional)</small></span><input type="text" id="ollama-name" maxlength="60" placeholder="Desktop GPU" value="${esc(ui.ollamaName)}"></label>
        <div class="form-actions"><button class="btn primary" type="submit">Add server</button></div>
      </form>${errLine("ollama-error")}</section>`;
  }

  function render() {
    if (disposed || !data) return;
    const focus = document.activeElement?.id;
    root.innerHTML = `<div class="providers">${claudeCard(data.claude)}${codexCard(data.codex)}${ollamaCard(data.ollama)}</div>`;
    if (focus && $(`#${focus}`, root)) $(`#${focus}`, root).focus();
    schedule();
  }

  function schedule() {
    clearTimeout(poll);
    const x = data.codex;
    const busy = ["downloading", "verifying", "unpacking"].includes(x.install?.state) || (x.login?.mode === "device" && x.login.running);
    if (busy) poll = setTimeout(refreshCodex, 1500);
  }

  async function load() {
    try {
      data = await api("/api/providers");
      if (ui.found === null) ui.found = (await api("/api/providers/ollama/discover")).found;
      render();
    } catch (ex) {
      if (!disposed) root.innerHTML = `<p class="error">${esc(ex.message)}</p>`;
    }
  }

  async function refreshCodex() {
    try {
      const before = data.codex;
      data.codex = await api("/api/providers/codex");
      if (before.install?.state !== "done" && data.codex.installed && !data.codex.linked && !data.codex.login) {
        return act("codex-login");  // installed just now: go straight to sign-in
      }
      if (!before.linked && data.codex.linked) toast("ChatGPT linked");
      render();
    } catch { schedule(); }
  }

  function showErr(id, ex) { const el = $(`#${id}`, root); if (el) el.textContent = ex.message; }

  async function act(what, el) {
    if (ui.busy[what]) return;
    ui.busy[what] = true;
    try {
      if (what === "claude-login") { data.claude.login = await api("/api/providers/claude/login", { method: "POST" }); }
      else if (what === "claude-cancel") { await api("/api/providers/claude/cancel", { method: "POST" }); data.claude.login = null; ui.claudeCode = ""; }
      else if (what === "claude-logout") { data.claude = await api("/api/providers/claude/logout", { method: "POST" }); }
      else if (what === "codex-install") { data.codex.install = await api("/api/providers/codex/install", { method: "POST" }); if (data.codex.install.state === "done") return refreshCodex(); }
      else if (what === "codex-login" || what === "codex-browser") {
        data.codex.login = await api("/api/providers/codex/login", { method: "POST", body: { mode: what === "codex-browser" ? "browser" : "device" } });
      }
      else if (what === "codex-cancel") { await api("/api/providers/codex/cancel", { method: "POST" }); data.codex.login = null; ui.codexUrl = ""; }
      else if (what === "codex-logout") { data.codex = await api("/api/providers/codex/logout", { method: "POST" }); }
      else if (what === "codex-copy") { try { await navigator.clipboard.writeText($("#codex-code", root).textContent); toast("Code copied"); } catch { /* select it instead */ } return; }
      else if (what === "ollama-remove") { await api(`/api/providers/ollama/${el.dataset.id}`, { method: "DELETE" }); data.ollama = await api("/api/providers/ollama"); }
      else if (what === "ollama-add-found") {
        await api("/api/providers/ollama", { method: "POST", body: { url: el.dataset.url } });
        ui.found = ui.found.filter((f) => f.url !== el.dataset.url);
        data.ollama = await api("/api/providers/ollama");
      }
      render();
    } catch (ex) {
      showErr(what.split("-")[0] + "-error", ex);
    } finally { ui.busy[what] = false; }
  }

  async function submit(what) {
    try {
      if (what === "claude-code") {
        ui.claudeCode = $("#claude-code", root).value;
        $("#claude-error", root).textContent = "Checking the code…";
        data.claude = await api("/api/providers/claude/code", { method: "POST", body: { code: ui.claudeCode } });
        ui.claudeCode = "";
        toast("Claude linked");
      } else if (what === "codex-callback") {
        ui.codexUrl = $("#codex-url", root).value;
        $("#codex-error", root).textContent = "Finishing sign-in…";
        data.codex = await api("/api/providers/codex/callback", { method: "POST", body: { url: ui.codexUrl } });
        ui.codexUrl = "";
        toast("ChatGPT linked");
      } else if (what === "ollama-add") {
        ui.ollamaUrl = $("#ollama-url", root).value;
        ui.ollamaName = $("#ollama-name", root).value;
        const t = await api("/api/providers/ollama/test", { method: "POST", body: { url: ui.ollamaUrl } });
        if (!t.ok) throw new Error(t.error);
        await api("/api/providers/ollama", { method: "POST", body: { url: ui.ollamaUrl, name: ui.ollamaName } });
        ui.ollamaUrl = ui.ollamaName = "";
        ui.found = (ui.found || []).filter((f) => f.url !== t.url);
        data.ollama = await api("/api/providers/ollama");
        toast(`Added Ollama with ${t.models.length} model${t.models.length === 1 ? "" : "s"}`);
      }
      render();
    } catch (ex) {
      showErr(what.split("-")[0] + "-error", ex);
    }
  }

  root.addEventListener("click", (e) => {
    const b = e.target.closest("[data-pa]");
    if (b) { e.preventDefault(); act(b.dataset.pa, b); }
  });
  root.addEventListener("submit", (e) => {
    const f = e.target.closest("[data-pf]");
    if (f) { e.preventDefault(); submit(f.dataset.pf); }
  });
  root.innerHTML = `<p class="muted">Checking what's linked…</p>`;
  load();
  return { dispose() { disposed = true; clearTimeout(poll); } };
}

/* ---------------- sign in with an OIDC provider (settings) ---------------- */

async function ssoCard(root) {
  let c;
  try { c = await api("/api/oidc"); } catch (ex) { root.innerHTML = `<p class="error">${esc(ex.message)}</p>`; return; }
  const redirect = c.redirect_uri || c.suggested_redirect_uri;
  const preset = (k) => c.presets[k] || c.presets.generic;
  const linked = c.linked;
  root.innerHTML = `<section class="card settings-card sso">
    <p class="muted">Sign in with Authentik, Microsoft, Google, Keycloak, Duo, or any OpenID Connect provider.
      Hangar still has one owner: you link your own identity at the provider, and only that identity can sign in.</p>
    ${linked ? `<p class="linked"><span class="ok-mark" aria-hidden="true">✓</span> Linked to <b>${esc(linked.email || linked.name || linked.sub)}</b>
        <span class="muted">at ${esc(linked.iss)}</span></p>` : ""}
    <form id="sso-form" class="form-stack sso-form">
      <div class="row2">
        <label class="field"><span>Provider</span><select id="sso-preset">${Object.entries(c.presets).map(([k, p]) =>
          `<option value="${k}"${k === c.preset ? " selected" : ""}>${esc(k === "generic" ? "Other (OpenID Connect)" : p.label)}</option>`).join("")}</select></label>
        <label class="field"><span>Button text <small>(optional)</small></span><input type="text" id="sso-label" maxlength="40" value="${esc(c.configured ? c.label : "")}" placeholder="${esc(preset(c.preset).label)}"></label>
      </div>
      <p class="hint" id="sso-help">${esc(preset(c.preset).help)}</p>
      <label class="field"><span>Issuer URL</span><input type="url" id="sso-issuer" required spellcheck="false" value="${esc(c.issuer)}" placeholder="${esc(preset(c.preset).issuer)}"></label>
      <div class="row2">
        <label class="field"><span>Client ID</span><input type="text" id="sso-client" required spellcheck="false" autocomplete="off" value="${esc(c.client_id)}"></label>
        <label class="field"><span>Client secret ${c.has_secret ? "<small>(saved; leave empty to keep)</small>" : ""}</span>
          <input type="password" id="sso-secret" autocomplete="new-password" spellcheck="false"></label>
      </div>
      <div class="field"><span>Redirect URI <small>(add this at the provider)</small></span>
        <div class="inline-form"><input type="text" id="sso-redirect" readonly value="${esc(redirect)}">
          <button class="btn" type="button" id="sso-copy">Copy</button></div>
        ${c.redirect_uri && c.redirect_uri !== c.suggested_redirect_uri ? `<p class="hint">You're on a different address now (${esc(c.suggested_redirect_uri)}). Save again to use it.</p>` : ""}
      </div>
      <div class="form-actions"><button class="btn${c.configured ? "" : " primary"}" type="submit">Save</button>
        ${c.configured ? `<a class="btn primary" href="/api/oidc/link">${linked ? "Link again" : `Link with ${esc(c.label)}`}</a>` : ""}
        ${c.configured ? `<button class="btn ghost" type="button" id="sso-remove">Remove</button>` : ""}</div>
      <p class="error" id="sso-error" role="alert"></p>
    </form>
    ${linked ? `<div class="settings-row">
      <div><b>Password sign-in</b><p class="muted">${c.password_login ? "On. You can sign in with your username and password, or with " + esc(c.label) + "."
        : "Off. Only " + esc(c.label) + " can sign in. If you get locked out, run <code>python -m hangar reset-password</code> on the server."}</p></div>
      <button class="btn" type="button" id="sso-pw">${c.password_login ? "Turn off password sign-in" : "Turn password sign-in back on"}</button></div>
      <div class="settings-row"><div><b>Unlink</b><p class="muted">Stops ${esc(c.label)} sign-in and turns password sign-in back on.</p></div>
      <button class="btn" type="button" id="sso-unlink">Unlink</button></div>` : ""}
  </section>`;

  $("#sso-preset", root).addEventListener("change", (e) => {
    const p = preset(e.target.value);
    $("#sso-help", root).textContent = p.help;
    $("#sso-issuer", root).placeholder = p.issuer;
    $("#sso-label", root).placeholder = p.label;
  });
  $("#sso-copy", root).addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#sso-redirect", root).value); toast("Copied"); } catch { $("#sso-redirect", root).select(); }
  });
  $("#sso-form", root).addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#sso-error", root).textContent = "Checking the provider…";
    try {
      await api("/api/oidc", { method: "PUT", body: { preset: $("#sso-preset", root).value, issuer: $("#sso-issuer", root).value,
        client_id: $("#sso-client", root).value, client_secret: $("#sso-secret", root).value || null,
        redirect_uri: c.suggested_redirect_uri, label: $("#sso-label", root).value } });
      toast("Saved. Now link your identity.");
      ssoCard(root);
    } catch (ex) { $("#sso-error", root).textContent = ex.message; }
  });
  $("#sso-remove", root)?.addEventListener("click", async () => {
    if (!confirm("Remove the sign-in provider? Password sign-in will be the only way in.")) return;
    await api("/api/oidc", { method: "DELETE" }).catch((ex) => toast(ex.message));
    ssoCard(root);
  });
  $("#sso-pw", root)?.addEventListener("click", async () => {
    if (c.password_login && !confirm(`Turn off password sign-in? You'll only be able to sign in with ${c.label}.`)) return;
    try { await api("/api/oidc/password-login", { method: "POST", body: { enabled: !c.password_login } }); ssoCard(root); }
    catch (ex) { toast(ex.message); }
  });
  $("#sso-unlink", root)?.addEventListener("click", async () => {
    await api("/api/oidc/unlink", { method: "POST" }).catch((ex) => toast(ex.message));
    ssoCard(root);
  });
}

/* ---------------- welcome (first run) ---------------- */

function welcomeView() {
  setMain(`${topbar("Welcome")}<div class="scroll"><div class="page welcome">
    <ol class="wizard" aria-label="Setup steps"><li class="done">Account</li><li class="on" aria-current="step">Connect AI</li><li>Start working</li></ol>
    <div class="page-head"><div><h1>Connect your AI</h1>
      <p class="muted">Link the services you want to use in sessions. You can skip any of them and come back in Settings.</p></div></div>
    <div id="welcome-providers"></div>
    <div class="welcome-foot"><button class="btn primary" id="welcome-done">Finish setup</button></div>
  </div></div>`);
  const cards = providerCards($("#welcome-providers"));
  $("#welcome-done").addEventListener("click", async () => {
    await api("/api/onboarding/done", { method: "POST" }).catch(() => {});
    state.me.onboarding = false;
    location.hash = "#/";
  });
  return cards;
}

/* ---------------- settings ---------------- */

function settingsView() {
  setMain(`${topbar("Settings")}<div class="scroll"><div class="page settings" id="settings">
    <div class="page-head"><h1>Settings</h1></div><p class="muted">Loading…</p></div></div>`);
  let disposed = false;
  let cards = null;

  function render(a) {
    const since = a.created ? new Date(a.created * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "";
    const envNote = a.env_password
      ? `<p class="note">HANGAR_PASSWORD is set in Hangar's environment. A password you set here stays until you change
         HANGAR_PASSWORD; after that, Hangar switches to the new value the next time it starts.</p>` : "";
    $("#settings").innerHTML = `
      <div class="page-head"><h1>Settings</h1></div>
      <section class="card settings-card" aria-labelledby="acct-h">
        <h2 id="acct-h">Account</h2>
        <p>Signed in as <b>${esc(a.username)}</b>${since ? ` <span class="muted">· owner since ${esc(since)}</span>` : ""}</p>
        ${envNote}
        <div class="settings-forms">
          <form id="pw-form" class="form-stack">
            <h3>Change password</h3>
            <input type="text" autocomplete="username" value="${esc(a.username)}" hidden aria-hidden="true">
            <label class="field"><span>Current password</span><input type="password" id="pw-current" autocomplete="current-password" required></label>
            <label class="field"><span>New password <small>(at least ${a.min_password} characters)</small></span>
              <input type="password" id="pw-new" autocomplete="new-password" minlength="${a.min_password}" required></label>
            <label class="field"><span>Confirm new password</span><input type="password" id="pw-confirm" autocomplete="new-password" required></label>
            <div class="form-actions"><button class="btn primary" type="submit">Change password</button></div>
            <p class="hint">Signs out every other device.</p>
            <p class="error" id="pw-error" role="alert"></p>
          </form>
          <form id="name-form" class="form-stack">
            <h3>Change username</h3>
            <label class="field"><span>New username</span>
              <input type="text" id="name-new" autocomplete="username" autocapitalize="none" spellcheck="false" value="${esc(a.username)}" required></label>
            <label class="field"><span>Password</span><input type="password" id="name-pw" autocomplete="current-password" required></label>
            <div class="form-actions"><button class="btn primary" type="submit">Save username</button></div>
            <p class="error" id="name-error" role="alert"></p>
          </form>
        </div>
        <div class="settings-row">
          <div><b>Sign out everywhere</b><p class="muted">Signs out every device except this one.</p></div>
          <button class="btn" id="signout-all" type="button">Sign out other devices</button>
        </div>
      </section>
      <h2 class="section-title" id="sso-h">Sign-in</h2>
      <div id="settings-sso" aria-labelledby="sso-h"></div>
      <h2 class="section-title" id="ai-h">AI providers</h2>
      <div id="settings-providers" aria-labelledby="ai-h"></div>`;
    ssoCard($("#settings-sso"));
    cards?.dispose();
    cards = providerCards($("#settings-providers"));

    $("#pw-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#pw-error");
      err.textContent = "";
      if ($("#pw-new").value !== $("#pw-confirm").value) { err.textContent = "The new passwords don't match."; return; }
      try {
        await api("/api/account/password", { method: "POST", body: { current: $("#pw-current").value, new: $("#pw-new").value } });
        e.target.reset();
        toast("Password changed. Other devices are signed out.");
      } catch (ex) { err.textContent = ex.message; }
    });
    $("#name-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#name-error");
      err.textContent = "";
      try {
        const r = await api("/api/account/username", { method: "POST", body: { username: $("#name-new").value, password: $("#name-pw").value } });
        state.me.user = r.username;
        toast(`Username is now ${r.username}`);
        load();
      } catch (ex) { err.textContent = ex.message; }
    });
    $("#signout-all").addEventListener("click", async () => {
      try {
        await api("/api/account/signout-all", { method: "POST" });
        toast("Other devices are signed out.");
      } catch (ex) { toast(ex.message); }
    });
  }

  async function load() {
    try {
      const a = await api("/api/account");
      if (!disposed) render(a);
    } catch (ex) {
      if (!disposed) $("#settings").innerHTML = `<div class="page-head"><h1>Settings</h1></div><p class="error">${esc(ex.message)}</p>`;
    }
  }
  load();
  return { dispose() { disposed = true; cards?.dispose(); } };
}

/* ---------------- dashboard ---------------- */

function dashboardView() {
  setMain(`${topbar("Hangar")}<div class="scroll"><div class="page" id="dash"></div></div>`);
  const previews = {};
  let disposed = false;

  async function loadPreviews() {
    const running = state.sessions.filter((s) => s.tmux).slice(0, 6);
    await Promise.all(running.map(async (s) => {
      try { previews[s.tmux.name] = (await api(`/api/tmux/${s.tmux.name}/preview?lines=14`)).text; } catch {}
    }));
    if (!disposed) render();
  }
  const pv = setInterval(() => !document.hidden && loadPreviews(), 5000);

  function usageCard(o) {
    const u = o.usage;
    if (!u && o.error) return `<div class="card span-5"><h2>Claude usage</h2><p class="muted">${esc(o.error)}</p></div>`;
    if (!u) return `<div class="card span-5"><h2>Claude usage</h2><p class="muted">Loading…</p></div>`;
    const meters = u.limits.map((l) => {
      const lv = level(l.utilization);
      const pace = l.pace != null && l.pace >= 100 && l.utilization >= 20;
      return `<div class="meter">
        <div class="meter-top"><span>${esc(l.label)}</span><b>${fmt.pct(l.utilization)}</b></div>
        <div class="bar"><i class="${lv.cls}" style="width:${Math.min(100, l.utilization)}%"></i></div>
        <div class="meter-sub"><span>${l.resets_at ? `${l.ends === "expires" ? "Expires" : "Resets"} in ${fmt.until(l.resets_at)} · ${fmt.when(l.resets_at)}` : ""}</span>
          <span>${lv.label ? `<span class="status-tag ${lv.cls}"><span class="sw" style="background:var(--${lv.cls})"></span>${lv.label}</span> ` : ""}${pace ? `<span class="status-tag serious" title="If you keep this rate up"><span class="sw" style="background:var(--serious)"></span>On pace for ${Math.round(l.pace)}%</span>` : ""}</span></div>
      </div>`;
    }).join("");
    const extra = u.extra_usage ? `<div class="meter-sub"><span>Extra usage: ${fmt.money(u.extra_usage.used_credits / 100)} of ${fmt.money(u.extra_usage.monthly_limit / 100)}</span></div>` : "";
    const plan = u.plan ? `<span class="badge">${esc(u.plan)}</span>` : "";
    return `<div class="card span-5"><h2><span>Claude usage</span><span>${plan}
      <button class="btn small ghost" id="usage-refresh" title="Updated ${esc(fmt.when(u.fetched_at))}">↻</button></span></h2>
      ${meters}${extra}${o.error ? `<p class="dim" style="font-size:12px;margin:8px 0 0">${esc(o.error)}</p>` : ""}</div>`;
  }

  function codexUsageCard(c) {
    const label = (m) => ({ 300: "5-hour limit", 10080: "Weekly limit", 43200: "30-day limit" }[m] || (m ? `${Math.round(m / 60)}-hour limit` : "Limit"));
    const meters = c.windows.map((w) => {
      const lv = level(w.used_percent);
      const resets = w.resets_at ? new Date(w.resets_at * 1000).toISOString() : null;
      return `<div class="meter">
        <div class="meter-top"><span>${esc(label(w.window_minutes))}</span><b>${fmt.pct(w.used_percent)}</b></div>
        <div class="bar"><i class="${lv.cls}" style="width:${Math.min(100, w.used_percent)}%"></i></div>
        <div class="meter-sub"><span>${resets ? `Resets in ${fmt.until(resets)} · ${fmt.when(resets)}` : ""}</span>
          <span>${lv.label ? `<span class="status-tag ${lv.cls}"><span class="sw" style="background:var(--${lv.cls})"></span>${lv.label}</span>` : ""}</span></div>
      </div>`;
    }).join("");
    const plan = c.plan ? `<span class="badge">${esc(c.plan)}</span>` : "";
    return `<div class="card span-5"><h2><span>ChatGPT usage <small class="dim">via Codex</small></span>${plan}</h2>
      ${meters || `<p class="muted">No limits reported.</p>`}
      <p class="dim" style="font-size:12px;margin:8px 0 0">As of the last Codex reply, ${esc(fmt.ago(c.as_of))}. It updates while you use Codex.</p></div>`;
  }

  function activityCard(st) {
    const days = st.days;
    const labels = days.map((d, i) => (i % 2 === (days.length - 1) % 2 ? new Date(d.date + "T12:00").toLocaleDateString(undefined, { weekday: "narrow" }) : ""));
    const tips = days.map((d) => `${new Date(d.date + "T12:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}<br><b>${fmt.tokens(d.io)}</b> input + output · ${d.messages} replies<br>${fmt.tokens(d.cache)} cache`);
    const t = st.today;
    return `<div class="card span-7"><h2><span>Token activity</span><span class="dim">this machine’s logs</span></h2>
      <div class="stat-row">
        <div class="stat"><div class="k">Today in / out</div><div class="v">${fmt.tokens(t.input + t.output)}</div><div class="s">${fmt.tokens(t.input)} in · ${fmt.tokens(t.output)} out</div></div>
        <div class="stat"><div class="k">Cache today</div><div class="v">${fmt.tokens(t.cache_read + t.cache_write)}</div><div class="s">${fmt.tokens(t.cache_read)} read</div></div>
        <div class="stat"><div class="k">Replies today</div><div class="v">${t.messages}</div><div class="s">${t.sessions} session${t.sessions === 1 ? "" : "s"}</div></div>
        <div class="stat"><div class="k">Last activity</div><div class="v" style="font-size:16px;padding-top:5px">${st.last_activity ? fmt.ago(st.last_activity) : "–"}</div></div>
      </div>
      <div style="margin-top:14px" class="dim">Input + output tokens per day, last 14 days</div>
      ${barChart(days.map((d) => d.io), { labels, tips, height: 120, highlightLast: true })}
      <details style="margin-top:6px"><summary class="dim" style="cursor:pointer;font-size:12px">Table view</summary>
        <table class="data"><tr><th>Day</th><th>In + out</th><th>Cache</th><th>Replies</th></tr>
        ${days.slice().reverse().map((d) => `<tr><td>${esc(d.date)}</td><td>${fmt.tokens(d.io)}</td><td>${fmt.tokens(d.cache)}</td><td>${d.messages}</td></tr>`).join("")}</table></details>
    </div>`;
  }

  function projectsCard(st) {
    const max = Math.max(1, ...st.projects.map((p) => p.tokens));
    const models = st.today_models.length ? `<div class="dim" style="margin:14px 0 6px">Models today</div><div class="chips">${st.today_models.map(([m, n]) => `<span class="badge">${esc(m)} · ${fmt.tokens(n)}</span>`).join("")}</div>` : "";
    return `<div class="card span-5"><h2>Projects, last 14 days</h2>
      <div class="list">${st.projects.map((p) => `<div class="list-row"><span class="name">${esc(p.name)}</span><span class="num">${fmt.tokens(p.tokens)}</span>
        <div class="hbar"><i style="width:${(p.tokens / max) * 100}%"></i></div></div>`).join("") || `<span class="dim">No activity yet.</span>`}</div>${models}</div>`;
  }

  function hoursCard(st) {
    const hours = st.today_hours;
    const nowH = new Date().getHours();
    const labels = hours.map((_, h) => (h % 6 === 0 ? (h === 0 ? "12a" : h === 12 ? "12p" : h < 12 ? h + "a" : h - 12 + "p") : ""));
    const tips = hours.map((v, h) => `${h}:00–${h}:59<br><b>${fmt.tokens(v)}</b> input + output`);
    return `<div class="card span-7"><h2><span>Today by hour</span><span class="dim">input + output tokens</span></h2>
      ${barChart(hours.map((v, h) => (h > nowH ? 0 : v)), { labels, tips, height: 150 })}</div>`;
  }

  function statusLine(o) {
    const s = o.status;
    if (!s) return "";
    const ok = s.indicator === "none";
    const inc = (s.incidents || []).map((i) => `<a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.name)}</a>`).join(", ");
    return `<span class="inc"><span class="dot" style="background:var(--${ok ? "good" : s.indicator === "minor" ? "warning" : "critical"})"></span>
      ${esc(s.description || "")}${inc ? " · " + inc : ""}</span>`;
  }

  function render() {
    const o = state.overview;
    const el = $("#dash");
    if (!el) return;
    const running = state.sessions.filter((s) => s.tmux);
    const runCards = running.map((s) => {
      const st = liveStatus(s);
      const text = (previews[s.tmux.name] || "").replace(/\s+$/, "").split("\n").slice(-14).join("\n");
      return `<a class="run-card" href="${esc(sessionHref(s))}"><header><span class="t">${esc(s.title)}</span>
        <span class="badge"><span class="dot ${st}"></span>${STATUS_TEXT[st]}</span></header><pre>${esc(text)}</pre></a>`;
    }).join("");
    el.innerHTML = `
      <div class="page-head"><div><h1>${esc(state.me?.hostname || "Hangar")}</h1>
        <div class="muted">${running.length} running · ${state.sessions.filter((s) => !s.pending).length} sessions on record ${o ? "· " + statusLine(o) : ""}</div></div>
        <button class="btn primary" data-act="new">+ New session</button></div>
      ${state.restorable && !running.length ? `<div class="notice"><span>${state.restorable} session${state.restorable > 1 ? "s were" : " was"} running when Hangar last stopped.</span><span><button class="btn small ghost" data-act="dismiss-restore">Dismiss</button> <button class="btn primary small" data-act="restore">Restore</button></span></div>` : ""}
      ${running.length ? `<div class="card" style="margin-bottom:14px"><h2>Running sessions</h2><div class="run-grid">${runCards}</div></div>` : ""}
      <div class="grid">
        ${o ? usageCard(o) : `<div class="card span-5"><h2>Claude usage</h2><p class="muted">Loading…</p></div>`}
        ${o ? activityCard(o.stats) : ""}
        ${o?.codex_usage ? codexUsageCard(o.codex_usage) : ""}
        ${o ? projectsCard(o.stats) : ""}
        ${o ? hoursCard(o.stats) : ""}
      </div>`;
    $("#usage-refresh")?.addEventListener("click", async () => { await api("/api/usage/refresh", { method: "POST" }); toast("Refreshing usage…"); });
  }

  render();
  loadPreviews();
  return {
    onOverview: render,
    onSessions() {
      // Fetch previews as soon as a running session shows up, instead of waiting for the next tick.
      if (state.sessions.some((s) => s.tmux && !(s.tmux.name in previews))) loadPreviews(); else render();
    },
    dispose() { disposed = true; clearInterval(pv); },
  };
}

/* ---------------- session view ---------------- */

function sessionView({ id, tmuxName }) {
  let disposed = false;
  let term = null;
  let tab = null;
  let lastTranscriptKey = "";
  let trTimer = null;
  let filesDir = null;      // folder shown in the Files tab
  let showHidden = false;

  const find = () => state.sessions.find((s) => (id ? s.id === id : s.tmux?.name === tmuxName));
  let s = find() || { id, title: "Loading…", tmux: tmuxName ? { name: tmuxName } : null, meta: {} };

  setMain(`${topbar(s.title)}<header class="s-head" id="s-head"></header><div id="s-body" style="flex:1;min-height:0;display:flex;flex-direction:column"></div>`);

  if (id && !find()) {
    api(`/api/sessions/${id}`).then((d) => { if (!disposed) { s = d; renderHead(); pickTab(); } })
      .catch(() => { if (!disposed) { setMain(`${topbar("Not found")}<div class="page"><h1>Session not found</h1><p class="muted">It may have been deleted. <a href="#/">Back to dashboard</a></p></div>`); } });
  }

  function renderHead() {
    const el = $("#s-head");
    if (!el) return;
    const live = s.tmux;
    const st = liveStatus(s);
    const tk = s.tokens ? s.tokens.input + s.tokens.output : null;
    const meta = [
      s.cwd && `<span title="${esc(s.cwd)}">${esc(s.cwd.replace(/^.*\/([^/]+\/[^/]+)$/, "…/$1"))}</span>`,
      s.git_branch && `<span>⎇ ${esc(s.git_branch)}</span>`,
      s.agent === "codex" && `<span class="tag">Codex${s.codex_mode ? ` · ${s.codex_mode === "full" ? "Full access" : "Asks before commands"}` : ""}</span>`,
      s.ollama && `<span class="tag" title="Runs on an Ollama model">Ollama · ${esc(s.ollama)}</span>`,
      s.models?.length && !s.ollama && `<span>${esc(s.models.join(", "))}</span>`,
      tk != null && `<span>${fmt.tokens(tk)} tokens in/out</span>`,
      s.cost != null && `<span>${fmt.money(s.cost)} API-equivalent</span>`,
      s.updated && `<span class="hide-sm">Updated ${fmt.ago(s.updated)}</span>`,
      live && `<span class="hide-sm">tmux: ${esc(live.name)}</span>`,
    ].filter(Boolean).join("");
    const actions = [];
    if (live) {
      actions.push(`<span class="badge"><span class="dot ${st}"></span>${STATUS_TEXT[st] || "Running"}</span>`);
    } else if (s.id && !s.pending) {
      actions.push(`<button class="btn primary small" id="resume">▶ Resume</button>`);
    }
    if (s.id && !s.pending) actions.push(`<button class="btn small" id="fork" title="Start a copy of this conversation with a new ID">Fork</button>`);
    if (s.id) {
      actions.push(`<button class="btn small" id="pin">${s.meta?.pinned ? "Unpin" : "Pin"}</button>`);
      actions.push(`<button class="btn small" id="archive">${s.meta?.archived ? "Unarchive" : "Archive"}</button>`);
      actions.push(`<button class="btn small ghost" id="copy" title="Copy the command to resume this session in any terminal">Copy resume cmd</button>`);
    }
    if (live) actions.push(`<button class="btn small danger" id="kill" title="Close the tmux session (the conversation stays resumable)">Stop</button>`);
    const tabs = [live && "terminal", s.id && !s.pending && "transcript", folder() && "files", s.id && "notes"].filter(Boolean);
    el.innerHTML = `<div class="s-head-top"><div style="min-width:0;flex:1">
        <h1>${s.id ? `<button id="rename" title="Rename">${esc(s.title)}</button>` : esc(s.title)}</h1>
        <div class="s-meta">${meta}</div></div><div class="s-actions">${actions.join("")}</div></div>
      <div class="tabs" role="tablist">${tabs.map((t) => `<button class="tab" role="tab" data-tab="${t}" aria-selected="${t === tab}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</div>`;
    const tb = $(".topbar .t");
    if (tb) tb.textContent = s.title;

    el.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
    $("#resume")?.addEventListener("click", () => resume(false));
    $("#fork")?.addEventListener("click", () => resume(true));
    $("#pin")?.addEventListener("click", () => saveMeta({ pinned: !s.meta?.pinned }));
    $("#archive")?.addEventListener("click", () => saveMeta({ archived: !s.meta?.archived }));
    $("#rename")?.addEventListener("click", rename);
    $("#copy")?.addEventListener("click", async () => {
      const cmd = s.agent === "codex" ? `cd ${JSON.stringify(s.cwd || ".")} && codex resume ${s.id}`
        : `cd ${JSON.stringify(s.cwd || ".")} && claude --resume ${s.id}`;
      try { await navigator.clipboard.writeText(cmd); toast("Copied"); } catch { prompt("Copy this command:", cmd); }
    });
    $("#kill")?.addEventListener("click", async () => {
      if (!confirm(`Stop this tmux session? ${agentName(s)} will exit, but you can resume the conversation later.`)) return;
      await api(`/api/tmux/${live.name}/kill`, { method: "POST" }).catch((e) => toast(e.message));
      term?.dispose(); term = null;
      await loadSessions();
    });
  }

  function folder() { return s.cwd || s.tmux?.cwd || ""; }

  /* Upload into the session folder and put @file references into Claude's prompt (not sent). */
  async function attach(fileList) {
    if (!folder()) return toast("This session has no folder");
    const done = await uploadAll(fileList, folder());
    if (!done.length) return;
    if (s.tmux && tab === "terminal") {
      const text = done.map((d) => mention(d.path, folder())).join(" ") + " ";
      await api(`/api/tmux/${s.tmux.name}/send`, { method: "POST", body: { text, enter: false } }).catch((e) => toast(e.message));
      term?.focus();
    }
    if (tab === "files") loadFiles();
  }

  async function saveMeta(fields) {
    try {
      const m = await api(`/api/sessions/${s.id}/meta`, { method: "PATCH", body: fields });
      s.meta = { name: m.name, pinned: !!m.pinned, archived: !!m.archived, notes: m.notes };
      if (fields.name !== undefined) s.title = m.name || s.ai_title || s.first_prompt || s.title;
      renderHead();
      loadSessions();
    } catch (e) { toast(e.message); }
  }

  function rename() {
    const name = prompt("Session name (leave empty to use the AI title):", s.meta?.name || s.title);
    if (name === null) return;
    saveMeta({ name: name.trim() });
  }

  async function resume(fork) {
    try {
      const r = await api(`/api/sessions/${s.id}/resume`, { method: "POST", body: { fork } });
      toast(fork ? "Forked into a new session" : r.already_running ? "Already running" : "Resumed in tmux");
      await loadSessions();
      if (fork) {
        // The fork gets a new ID once Claude writes its log; open it by tmux name until then.
        location.hash = `#/t/${r.name}`;
      } else {
        s = find() || s;
        tab = "terminal";
        renderHead();
        showTab("terminal");
      }
    } catch (e) { toast(e.message); }
  }

  function pickTab() {
    const want = state.sessionTab[id || tmuxName];
    if (want && (want !== "terminal" || s.tmux)) return showTab(want);
    showTab(s.tmux ? "terminal" : s.id && !s.pending ? "transcript" : "notes");
  }

  function showTab(name) {
    if (name === tab && $("#s-body").childElementCount) return;
    tab = name;
    state.sessionTab[id || tmuxName] = name;
    clearInterval(trTimer);
    if (name !== "terminal" && term) { term.dispose(); term = null; }
    renderHead();
    const body = $("#s-body");
    if (name === "terminal") {
      body.innerHTML = `<div class="term-wrap"><div class="term" id="term"></div>
        <div class="keybar" aria-label="Keys">${KEYS.map(([l, , t]) => `<button data-key="${esc(l)}" title="${esc(t || l)}">${esc(l)}</button>`).join("")}
          <button data-key="attach" title="Upload files to the session folder and mention them in the prompt">+ Attach</button>
          <button data-key="compose" title="Type or paste a longer message">✎ Type</button></div>
        <input type="file" id="attach-input" multiple hidden>
        <form class="compose" id="compose" hidden><textarea id="compose-text" rows="1" placeholder="Message to send to the session (Enter sends, Shift+Enter for a new line)"></textarea>
          <button class="btn primary" type="submit">Send</button></form></div>`;
      term = new TermClient($("#term"), s.tmux.name, () => { s.tmux = null; loadSessions(); });
      body.querySelector(".keybar").addEventListener("click", (e) => {
        const b = e.target.closest("[data-key]");
        if (!b) return;
        if (b.dataset.key === "attach") { $("#attach-input").click(); return; }
        if (b.dataset.key === "compose") {
          const f = $("#compose"); f.hidden = !f.hidden; if (!f.hidden) $("#compose-text").focus(); term.fit(); return;
        }
        const k = KEYS.find(([l]) => l === b.dataset.key);
        term.send(k[1]);
        term.focus();
      });
      $("#attach-input").addEventListener("change", (e) => { const f = e.target.files; if (f.length) attach(f); e.target.value = ""; });
      const ta = $("#compose-text");
      ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#compose").requestSubmit(); } });
      ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(160, ta.scrollHeight) + "px"; });
      $("#compose").addEventListener("submit", async (e) => {
        e.preventDefault();
        const text = ta.value;
        if (!text.trim()) return;
        try {
          await api(`/api/tmux/${s.tmux.name}/send`, { method: "POST", body: { text, enter: true } });
          ta.value = ""; ta.style.height = "auto";
        } catch (err) { toast(err.message); }
      });
    } else if (name === "transcript") {
      lastTranscriptKey = "";
      body.innerHTML = `<div class="scroll" id="tr-scroll"><div class="transcript" id="tr"><p class="muted">Loading…</p></div></div>`;
      loadTranscript();
      trTimer = setInterval(() => !document.hidden && s.tmux && loadTranscript(), 5000);
    } else if (name === "files") {
      if (!filesDir) filesDir = folder();
      body.innerHTML = `<div class="scroll" id="files-scroll"><div class="files">
        <div class="files-bar"><div class="files-path" id="files-path"></div>
          <label class="dim files-hidden"><input type="checkbox" id="files-hidden" ${showHidden ? "checked" : ""}> Hidden</label>
          <button class="btn small" id="files-refresh" title="Refresh">Refresh</button>
          <button class="btn primary small" id="files-upload">Upload</button>
          <input type="file" id="files-input" multiple hidden></div>
        <div class="dropzone" id="dropzone">Drop files anywhere here to upload them to this folder</div>
        <div id="files-list"><p class="muted">Loading…</p></div></div></div>`;
      $("#files-upload").addEventListener("click", () => $("#files-input").click());
      $("#files-input").addEventListener("change", async (e) => {
        const f = e.target.files; if (!f.length) return;
        await uploadAll(f, filesDir); e.target.value = ""; loadFiles();
      });
      $("#files-refresh").addEventListener("click", loadFiles);
      $("#files-hidden").addEventListener("change", (e) => { showHidden = e.target.checked; loadFiles(); });
      loadFiles();
    } else if (name === "notes") {
      body.innerHTML = `<div class="scroll"><div class="notes"><label for="notes" class="dim">Private notes for this session (saved in Hangar, not sent to Claude)</label>
        <textarea id="notes" placeholder="What was this for? What's left to do?">${esc(s.meta?.notes || "")}</textarea>
        <div><button class="btn primary small" id="save-notes">Save notes</button></div></div></div>`;
      $("#save-notes").addEventListener("click", async () => { await saveMeta({ notes: $("#notes").value }); toast("Notes saved"); });
    }
  }

  async function loadFiles() {
    const el = $("#files-list");
    if (!el) return;
    let d;
    try { d = await api(`/api/files?path=${encodeURIComponent(filesDir)}`); }
    catch (e) { el.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
    if (disposed || !$("#files-list")) return;
    filesDir = d.path;
    const root = folder();
    const rel = d.path === root ? "" : d.path.startsWith(root + "/") ? d.path.slice(root.length + 1) : null;
    $("#files-path").innerHTML = (d.parent ? `<button class="btn small ghost" data-go="${esc(d.parent)}" title="Up one folder">↑ Up</button>` : "")
      + `<span title="${esc(d.path)}">${rel === null ? esc(d.path) : rel === "" ? "Session folder" : `Session folder / ${esc(rel)}`}</span>`
      + (rel !== "" ? ` <button class="btn small ghost" data-go="${esc(root)}">Back to session folder</button>` : "");
    const entries = d.entries.filter((e) => showHidden || !e.name.startsWith("."));
    const href = (e, inline) => `/api/files/download?path=${encodeURIComponent(d.path + "/" + e.name)}${inline ? "&inline=true" : ""}`;
    const canMention = !!s.tmux;
    el.innerHTML = entries.length ? `<table class="file-list"><thead><tr><th>Name</th><th class="hide-sm">Modified</th><th>Size</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>
      ${entries.map((e) => `<tr>
        <td class="fname">${e.dir ? `<button class="linkish" data-go="${esc(d.path + "/" + e.name)}"><span class="ficon">▸</span>${esc(e.name)}/</button>`
          : `<a href="${href(e, true)}" target="_blank" rel="noopener">${esc(e.name)}</a>`}</td>
        <td class="dim hide-sm">${fmt.ago(e.mtime)}</td><td class="dim">${e.dir ? "" : fmt.size(e.size)}</td>
        <td class="factions">${e.dir ? "" : `<a class="btn small ghost" href="${href(e)}" download title="Download">Download</a>`}
          ${canMention ? `<button class="btn small ghost" data-mention="${esc(d.path + "/" + e.name)}" title="Type a reference to this into Claude's prompt">@ Mention</button>` : ""}</td></tr>`).join("")}
      </tbody></table>${d.truncated ? `<p class="muted">Showing the first 2000 items.</p>` : ""}`
      : `<p class="muted">This folder is empty${!showHidden && d.entries.length ? " (hidden files not shown)" : ""}.</p>`;
    $("#files-scroll").querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => { filesDir = b.dataset.go; loadFiles(); }));
    el.querySelectorAll("[data-mention]").forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/tmux/${s.tmux.name}/send`, { method: "POST", body: { text: mention(b.dataset.mention, root) + " ", enter: false } })
        .then(() => toast("Added to the prompt. Open the Terminal tab to finish your message.")).catch((e) => toast(e.message));
    }));
  }

  // Drag and drop: Files tab uploads to the shown folder; elsewhere to the session folder (+ mention in the terminal).
  const sbody = $("#s-body");
  let dragDepth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  sbody.addEventListener("dragenter", (e) => { if (!hasFiles(e) || !folder()) return; e.preventDefault(); dragDepth++; sbody.classList.add("dragging"); });
  sbody.addEventListener("dragover", (e) => { if (hasFiles(e) && folder()) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } });
  sbody.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; sbody.classList.remove("dragging"); } });
  sbody.addEventListener("drop", async (e) => {
    if (!hasFiles(e) || !folder()) return;
    e.preventDefault(); dragDepth = 0; sbody.classList.remove("dragging");
    const f = e.dataTransfer.files;
    if (!f.length) return;
    if (tab === "files") { await uploadAll(f, filesDir); loadFiles(); } else attach(f);
  });

  async function loadTranscript() {
    if (!s.id) return;
    const data = await api(`/api/sessions/${s.id}/transcript`).catch(() => null);
    const el = $("#tr");
    if (!data || !el || disposed) return;
    const key = data.items.length + ":" + (data.items.at(-1)?.text || data.items.at(-1)?.result || "").length;
    if (key === lastTranscriptKey) return;
    lastTranscriptKey = key;
    const sc = $("#tr-scroll");
    const atBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 80 || !el.dataset.done;
    el.innerHTML = renderTranscript(data.items, agentName(s)) || `<p class="muted">No messages yet.</p>`;
    el.dataset.done = "1";
    if (atBottom) sc.scrollTop = sc.scrollHeight;
  }

  renderHead();
  if (find()) pickTab();

  return {
    onSessions() {
      const fresh = find();
      if (!fresh) return;
      const wasLive = !!s.tmux;
      s = fresh;
      renderHead();
      if (wasLive !== !!s.tmux) {
        if (s.tmux && tab !== "terminal") showTab("terminal");
        else if (!s.tmux && tab === "terminal") showTab(s.id && !s.pending ? "transcript" : "notes");
      } else if (!tab) pickTab();
    },
    onTheme() { term?.applyTheme(); },
    dispose() { disposed = true; clearInterval(trTimer); term?.dispose(); },
  };
}

function renderTranscript(items, who = "Claude") {
  const md = (t) => DOMPurify.sanitize(marked.parse(t, { breaks: false, gfm: true }));
  let html = "";
  let toolRun = [];
  const flush = () => {
    if (!toolRun.length) return;
    html += `<div class="tool-run">${toolRun.map((t) => `<details class="tool"><summary><span class="tname">${esc(t.name)}</span>
      <span class="tsum">${esc(t.summary)}</span>${t.error ? `<span class="terr">error</span>` : ""}</summary>
      <pre>${esc(t.input)}</pre>${t.result != null ? `<pre>${esc(t.result)}</pre>` : ""}</details>`).join("")}</div>`;
    toolRun = [];
  };
  for (const it of items) {
    if (it.role === "tool") { toolRun.push(it); continue; }
    flush();
    const time = it.ts ? new Date(it.ts).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
    if (it.role === "user") {
      html += `<div class="msg user"><div class="who"><span>You</span><span>${esc(time)}</span></div><div class="body">${esc(it.text)}</div></div>`;
    } else {
      html += `<div class="msg assistant"><div class="who"><span>${esc(who)}</span><span>${esc(time)}</span></div><div class="body">${md(it.text)}</div></div>`;
    }
  }
  flush();
  return html;
}

/* ---------------- terminal ---------------- */

const KEYS = [
  ["Esc", "\x1b", "Escape (interrupt Claude)"], ["Tab", "\t"], ["⇧Tab", "\x1b[Z", "Shift+Tab (switch mode)"],
  ["↑", "\x1b[A"], ["↓", "\x1b[B"], ["←", "\x1b[D"], ["→", "\x1b[C"], ["⏎", "\r", "Enter"],
  ["^C", "\x03", "Ctrl+C"], ["^D", "\x04", "Ctrl+D"], ["^R", "\x12", "Ctrl+R"], ["^O", "\x0f", "Ctrl+O (transcript)"],
  ["PgUp", "\x1b[5~"], ["PgDn", "\x1b[6~"], ["y", "y"], ["n", "n"], ["1", "1"], ["2", "2"], ["3", "3"],
];

const TERM_THEME = {
  background: "#14161b", foreground: "#d8dae0", cursor: "#d8dae0", cursorAccent: "#14161b",
  selectionBackground: "#3b4252", black: "#1c1f26", brightBlack: "#5c6370", red: "#e06c75", brightRed: "#ef8891",
  green: "#98c379", brightGreen: "#b5e090", yellow: "#e5c07b", brightYellow: "#f0d197", blue: "#61afef",
  brightBlue: "#8cc5f5", magenta: "#c678dd", brightMagenta: "#d79ae8", cyan: "#56b6c2", brightCyan: "#7fd0db",
  white: "#d8dae0", brightWhite: "#ffffff",
};

class TermClient {
  constructor(el, name, onGone) {
    this.el = el;
    this.name = name;
    this.onGone = onGone;
    this.retry = 0;
    this.closed = false;
    const small = innerWidth < 600;
    this.term = new Terminal({
      fontFamily: '"JetBrains Mono", "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace',
      fontSize: small ? 11 : 13.5, lineHeight: 1.12, cursorBlink: true, scrollback: 5000,
      allowProposedApi: true, theme: TERM_THEME, macOptionIsMeta: true,
    });
    this.fitAddon = new FitAddon.FitAddon();
    this.term.loadAddon(this.fitAddon);
    this.term.loadAddon(new WebLinksAddon.WebLinksAddon());
    this.term.open(el);
    this.enc = new TextEncoder();
    this.term.onData((d) => this.send(d));
    this.term.onBinary((d) => this.sendBytes(Uint8Array.from(d, (c) => c.charCodeAt(0))));
    this.term.onResize(({ cols, rows }) => this.ctl({ type: "resize", cols, rows }));
    this.ro = new ResizeObserver(() => this.fit());
    this.ro.observe(el);
    this.fit();
    this.connect();
    if (!small) this.term.focus();
  }
  applyTheme() { this.term.options.theme = TERM_THEME; }
  fit() { try { this.fitAddon.fit(); } catch {} }
  focus() { this.term.focus(); }
  overlay(html) {
    let o = this.el.parentElement.querySelector(".term-overlay");
    if (!html) { o?.remove(); return; }
    if (!o) { o = document.createElement("div"); o.className = "term-overlay"; this.el.parentElement.appendChild(o); }
    o.innerHTML = `<div class="box">${html}</div>`;
  }
  connect() {
    if (this.closed) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const { cols, rows } = this.term;
    const ws = new WebSocket(`${proto}://${location.host}/ws/term/${encodeURIComponent(this.name)}?cols=${cols}&rows=${rows}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => { this.retry = 0; this.overlay(null); this.term.reset(); this.fit(); this.ctl({ type: "resize", cols: this.term.cols, rows: this.term.rows }); };
    ws.onmessage = (e) => {
      if (typeof e.data === "string") {
        try { if (JSON.parse(e.data).type === "gone") { this.gone = true; } } catch {}
        return;
      }
      this.term.write(new Uint8Array(e.data));
    };
    ws.onclose = (e) => {
      if (this.closed) return;
      if (this.gone) {
        this.overlay(`<b>This tmux session has ended.</b><span>The conversation is saved; use Resume to pick it back up.</span>`);
        this.onGone?.();
        return;
      }
      if (e.code === 4401) { showLogin(); return; }
      this.retry++;
      const wait = Math.min(10000, 500 * 2 ** this.retry);
      // A clean close with no error usually means the session exited.
      if (e.code === 1000 && this.retry > 2) {
        this.overlay(`<b>Session closed.</b><button class="btn small" id="term-reconnect">Reconnect</button>`);
        this.el.parentElement.querySelector("#term-reconnect")?.addEventListener("click", () => { this.retry = 0; this.connect(); });
        this.onGone?.();
        return;
      }
      this.overlay(`<b>Disconnected</b><span>Reconnecting…</span>`);
      this.timer = setTimeout(() => this.connect(), wait);
    };
  }
  send(str) { this.sendBytes(this.enc.encode(str)); }
  sendBytes(bytes) { if (this.ws?.readyState === 1) this.ws.send(bytes); }
  ctl(obj) { if (this.ws?.readyState === 1) this.ws.send(JSON.stringify(obj)); }
  dispose() {
    this.closed = true;
    clearTimeout(this.timer);
    this.ro.disconnect();
    try { this.ws?.close(); } catch {}
    this.term.dispose();
  }
}

/* ---------------- new session dialog ---------------- */

async function openNewSession() {
  toggleSide(false);
  const dlg = $("#dialog");
  const recent = [...new Set(state.sessions.map((s) => s.cwd).filter(Boolean))].slice(0, 8);
  let cwd = recent[0] || state.me?.workspaces?.[0] || "";
  const avail = { codex: null, ollama: null };  // filled in below
  dlg.innerHTML = `<form method="dialog" id="new-form">
    <h2>New session</h2>
    <div class="field"><span>Folder</span>
      ${recent.length ? `<div class="chips">${recent.map((p) => `<button type="button" class="chip" data-cwd="${esc(p)}" title="${esc(p)}">${esc(p.split("/").pop() || p)}</button>`).join("")}</div>` : ""}
      <div class="picker"><div class="picker-path"><button type="button" class="btn small ghost" id="pk-up" aria-label="Up one folder">↑</button><span id="pk-path"></span></div>
      <div class="picker-list" id="pk-list"></div></div></div>
    <div class="field"><span>Name <small>(optional)</small></span><input type="text" id="nf-label" maxlength="60" placeholder="e.g. Fix login bug"></div>
    <fieldset class="field agent-pick"><legend>Agent</legend>
      <label class="seg"><input type="radio" name="nf-agent" value="claude" checked><span>Claude Code</span></label>
      <label class="seg"><input type="radio" name="nf-agent" value="codex"><span>Codex</span></label>
      <label class="seg"><input type="radio" name="nf-agent" value="shell"><span>Plain shell</span></label>
    </fieldset>
    <p class="hint" id="nf-agent-note"></p>
    <div class="row2" id="nf-opts">
      <label class="field"><span>Model</span><select id="nf-model"></select></label>
      <label class="field" id="nf-perm-wrap"><span>Permission mode</span><select id="nf-perm"><option value="">Default</option><option value="acceptEdits">Accept edits</option><option value="plan">Plan</option><option value="auto">Auto</option><option value="bypassPermissions">Bypass permissions</option></select></label>
      <label class="field" id="nf-mode-wrap" hidden><span>Commands</span><select id="nf-mode">
        <option value="ask">Ask before commands</option><option value="full">Full access, don't ask</option></select></label>
    </div>
    <p class="hint" id="nf-mode-note" hidden></p>
    <p class="error" id="nf-err"></p>
    <div class="dialog-actions"><button type="button" class="btn" id="nf-cancel">Cancel</button><button class="btn primary" id="nf-go" type="submit">Start session</button></div>
  </form>`;
  dlg.showModal();

  const agent = () => dlg.querySelector("[name=nf-agent]:checked").value;
  const MODE_NOTES = {
    ask: "Codex's sandbox can't run inside Docker, so each command first fails there and Codex then asks you to run it for real.",
    full: "Codex runs commands without asking. The container is the only boundary.",
  };

  function ollamaOptions() {
    const servers = (avail.ollama || []).filter((sv) => sv.ok && sv.models.length);
    return servers.map((sv) => `<optgroup label="Ollama · ${esc(sv.name)}">${sv.models.map((m) =>
      `<option value="ollama:${esc(sv.id)}:${esc(m.name)}">${esc(m.name)}${m.size ? ` (${esc(m.size)})` : ""}</option>`).join("")}</optgroup>`).join("");
  }

  function syncAgent() {
    const a = agent();
    const x = avail.codex;
    $("#nf-opts").hidden = a === "shell";
    $("#nf-perm-wrap").hidden = a !== "claude";
    $("#nf-mode-wrap").hidden = a !== "codex";
    $("#nf-mode-note").hidden = a !== "codex";
    $("#nf-mode-note").textContent = MODE_NOTES[$("#nf-mode").value];
    const keep = $("#nf-model").value;
    const own = a === "codex"
      ? (x?.linked ? `<option value="">Default (ChatGPT)</option>` : "")
      : `<option value="">Default</option><option value="opus">Opus</option><option value="sonnet">Sonnet</option><option value="haiku">Haiku</option>`;
    $("#nf-model").innerHTML = own + ollamaOptions();
    if ([...$("#nf-model").options].some((o) => o.value === keep)) $("#nf-model").value = keep;
    let note = "";
    if (a === "codex") {
      if (!x) note = "Checking Codex…";
      else if (!x.installed) note = `Codex isn't installed yet. <a href="#/settings">Link ChatGPT in Settings</a>.`;
      else if (!x.linked && !$("#nf-model").options.length) note = `Sign in with ChatGPT in <a href="#/settings">Settings</a>, or add an Ollama server to use Codex with a local model.`;
      else if (!x.linked) note = "Not signed in to ChatGPT, so Codex can only use Ollama models.";
    }
    if (avail.ollama === null && a !== "shell") note += `${note ? " " : ""}Looking for Ollama models…`;
    $("#nf-agent-note").innerHTML = note;
    $("#nf-go").disabled = a === "codex" && (!x?.installed || !$("#nf-model").options.length);
  }
  dlg.querySelectorAll("[name=nf-agent]").forEach((r) => r.addEventListener("change", syncAgent));
  $("#nf-mode").addEventListener("change", syncAgent);
  $("#nf-agent-note").addEventListener("click", (e) => { if (e.target.closest("a")) dlg.close(); });
  syncAgent();
  api("/api/providers/codex").then((x) => { avail.codex = x; syncAgent(); }).catch(() => { avail.codex = { installed: false }; syncAgent(); });
  api("/api/providers/ollama").then((o) => { avail.ollama = o.servers; syncAgent(); }).catch(() => { avail.ollama = []; syncAgent(); });

  async function browse(path) {
    try {
      const d = await api("/api/dirs?path=" + encodeURIComponent(path || ""));
      if (d.path) cwd = d.path;
      $("#pk-path").textContent = d.path ? "‎" + d.path : "Workspaces";
      $("#pk-up").disabled = !d.parent && !d.path;
      $("#pk-up").onclick = () => browse(d.parent || "");
      $("#pk-list").innerHTML = d.dirs.map((x) => `<button type="button" data-path="${esc(x.path)}">${esc(x.name)}</button>`).join("") || `<div class="empty">No subfolders</div>`;
    } catch (e) {
      $("#nf-err").textContent = e.message;
      if (path) browse("");
    }
  }
  $("#pk-list").addEventListener("click", (e) => { const b = e.target.closest("[data-path]"); if (b) browse(b.dataset.path); });
  dlg.querySelectorAll("[data-cwd]").forEach((b) => b.addEventListener("click", () => browse(b.dataset.cwd)));
  $("#nf-cancel").addEventListener("click", () => dlg.close());
  browse(cwd);

  $("#new-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!cwd) { $("#nf-err").textContent = "Pick a folder."; return; }
    const a = agent();
    const model = $("#nf-model").value;
    const body = { cwd, label: $("#nf-label").value, shell: a === "shell", agent: a === "codex" ? "codex" : "claude" };
    if (model.startsWith("ollama:")) {
      const [, server, ...rest] = model.split(":");
      Object.assign(body, { ollama_server: server, ollama_model: rest.join(":") });
    } else body.model = model;
    if (a === "claude") body.permission_mode = $("#nf-perm").value;
    if (a === "codex") body.codex_mode = $("#nf-mode").value;
    $("#nf-go").disabled = true;
    try {
      const r = await api("/api/tmux/new", { method: "POST", body });
      dlg.close();
      await loadSessions();
      location.hash = r.claude_id ? `#/s/${r.claude_id}` : `#/t/${r.name}`;
    } catch (err) {
      $("#nf-err").textContent = err.message;
      $("#nf-go").disabled = false;
    }
  });
}

/* ---------------- plugins ---------------- */

// Optional plugins (HANGAR_PLUGINS) add a sidebar link, a script, and hash routes through window.hangar.
const pluginRoutes = [];
window.hangar = {
  api, esc, toast, fmt, state, topbar, setMain, toggleSide, loadSessions,
  registerRoute(pattern, view) { pluginRoutes.push({ pattern, view }); },
};

function renderSideNav() {
  const nav = $("#side-nav");
  const links = (state.me?.plugins || []).filter((p) => p.nav);
  nav.hidden = !links.length;
  nav.innerHTML = links.map((p) => {
    const active = location.hash === p.nav.href || location.hash.startsWith(p.nav.href + "/");
    return `<a class="side-link${active ? " active" : ""}" href="${esc(p.nav.href)}">${esc(p.nav.label)}</a>`;
  }).join("");
}
$("#side-nav").addEventListener("click", (e) => { if (e.target.closest("a")) toggleSide(false); });

const loadedPlugins = new Set();
async function loadPlugins() {
  await Promise.all((state.me?.plugins || []).filter((p) => !loadedPlugins.has(p.name)).map((p) => {
    loadedPlugins.add(p.name);
    if (p.style) document.head.insertAdjacentHTML("beforeend", `<link rel="stylesheet" href="${esc(p.style)}">`);
    if (!p.script) return null;
    return new Promise((resolve) => {
      const el = document.createElement("script");
      el.src = p.script;
      el.onload = resolve;
      el.onerror = () => { toast(`Couldn't load the ${p.name} plugin`); resolve(); };
      document.body.appendChild(el);
    });
  }));
}

/* ---------------- boot ---------------- */

async function boot() {
  try {
    state.me = await api("/api/me");
  } catch { return; }
  $("#login").hidden = true;
  $("#app").hidden = false;
  if (takeQuery("oidc") === "linked") toast("Sign-in provider linked");
  const err = takeQuery("oidc_error");
  if (err) toast(err, 6000);
  await loadPlugins();
  startPolling();
  if (state.me.onboarding && location.hash !== "#/welcome") location.hash = "#/welcome";
  else route();
}

boot();
