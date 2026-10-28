/* Hangar Ideas: project tracker UI. Plain JS, no build step. Loaded by Hangar when HANGAR_PLUGINS includes "ideas". */
(() => {
  "use strict";
  const H = window.hangar;
  const { api, esc, toast, fmt } = H;
  const BASE = "/api/ideas";
  const $ = (sel, root = document) => root.querySelector(sel);

  // Everything stored in Ideas is user content (some written by Claude): always escape or sanitize it.
  // No images or other embeds: a URL in one would load without a click, which could leak data to another site.
  const PURIFY = {
    FORBID_TAGS: ["img", "picture", "source", "video", "audio", "iframe", "object", "embed", "svg", "math", "form",
      "input", "button", "textarea", "select", "style", "link", "meta"],
    FORBID_ATTR: ["style", "srcset"],
  };
  const md = (text) => DOMPurify.sanitize(marked.parse(text || "", { gfm: true, breaks: true }), PURIFY);

  const STATUSES = [
    ["active", "Active"], ["planned", "Planned"], ["idea", "Ideas"],
    ["paused", "Paused"], ["shipped", "Shipped"], ["killed", "Killed"],
  ];
  const STATUS_LABEL = { idea: "Idea", planned: "Planned", active: "Active", paused: "Paused", shipped: "Shipped", killed: "Killed" };
  const M_LABEL = { todo: "To do", doing: "Doing", done: "Done", dropped: "Dropped" };
  const POLL_MS = 8000;

  const ui = { q: "", tag: "" };  // list filters, kept while moving between views

  // Don't re-render under someone's fingers: skip a refresh while they're typing or a dialog is open.
  const isEditing = (strict = false) => {
    if ($("#dialog").open || $("#i-summary-text")) return true;
    const el = document.activeElement;
    return !!el && $("#main").contains(el) && el.matches("input, textarea, select") && (strict || !!el.value);
  };

  function progress(p) {
    const { done, total } = p.progress;
    const pct = total ? Math.round((done / total) * 100) : 0;
    return `<span class="i-progress" title="${done} of ${total} milestones done"><span class="bar"><i style="width:${pct}%"></i></span>${done}/${total}</span>`;
  }

  const tagChips = (tags) => tags.map((t) => `<span class="badge">${esc(t)}</span>`).join("");

  /* ---------------- list ---------------- */

  function listView() {
    let disposed = false;
    let projects = null;
    let error = "";
    H.setMain(`${H.topbar("Ideas")}<div class="scroll"><div class="page ideas" id="ideas"></div></div>`);

    async function load() {
      try {
        const data = await api(`${BASE}/projects${ui.q ? "?q=" + encodeURIComponent(ui.q) : ""}`);
        projects = data.projects;
        error = "";
      } catch (e) { error = e.message; }
      if (!disposed) render();
    }

    function row(p) {
      const next = p.next_milestone ? `Next: ${p.next_milestone.title}` : p.progress.total ? "All milestones done" : "No milestones yet";
      return `<a class="i-row" href="#/ideas/p/${p.id}">
        <span class="i-title">${esc(p.title)}</span>
        ${progress(p)}
        <span class="i-sub">${esc(next)}</span>
        <span class="i-meta">${tagChips(p.tags)}<span class="dim">${esc(fmt.ago(p.last_activity_at))}</span></span></a>`;
    }

    function render() {
      const el = $("#ideas");
      if (!el) return;
      const head = `<div class="page-head"><div><h1>Ideas</h1>
          <div class="muted">${projects ? `${projects.length} project${projects.length === 1 ? "" : "s"}` : "Loading…"}</div></div>
        <div class="i-head-actions"><button class="btn" id="i-connect">Connect Claude Code</button>
        <button class="btn primary" id="i-new">+ New idea</button></div></div>`;
      if (!el.dataset.ready) {
        el.innerHTML = `${head}<div class="i-filters">
            <input type="search" id="i-q" placeholder="Search ideas" aria-label="Search ideas" autocomplete="off" value="${esc(ui.q)}">
            <select id="i-tag" aria-label="Filter by tag"><option value="">All tags</option></select></div>
          <div id="i-body"></div>`;
        el.dataset.ready = "1";
        $("#i-new").addEventListener("click", newIdeaDialog);
        $("#i-connect").addEventListener("click", connectDialog);
        $("#i-q").addEventListener("input", (e) => {
          ui.q = e.target.value.trim();
          clearTimeout(render._t);
          render._t = setTimeout(load, 200);
        });
        $("#i-tag").addEventListener("change", (e) => { ui.tag = e.target.value; render(); });
      } else {
        el.querySelector(".page-head").outerHTML = head;
        $("#i-new").addEventListener("click", newIdeaDialog);
        $("#i-connect").addEventListener("click", connectDialog);
      }

      const tags = [...new Set((projects || []).flatMap((p) => p.tags))].sort((a, b) => a.localeCompare(b));
      const sel = $("#i-tag");
      const want = tags.join("\n");
      if (sel.dataset.opts !== want) {
        sel.innerHTML = `<option value="">All tags</option>` + tags.map((t) => `<option>${esc(t)}</option>`).join("");
        sel.dataset.opts = want;
      }
      if (!tags.includes(ui.tag)) ui.tag = "";
      sel.value = ui.tag;
      sel.hidden = !tags.length;

      const body = $("#i-body");
      if (error) { body.innerHTML = `<p class="error">${esc(error)}</p>`; return; }
      if (!projects) { body.innerHTML = `<p class="muted">Loading…</p>`; return; }
      const items = projects.filter((p) => !ui.tag || p.tags.includes(ui.tag));
      if (!items.length) {
        body.innerHTML = ui.q || ui.tag ? `<div class="empty">No ideas match.</div>` : emptyState();
        return;
      }
      body.innerHTML = STATUSES.map(([status, label]) => {
        const group = items.filter((p) => p.status === status);
        if (!group.length) return "";
        const rows = group.map(row).join("");
        if (status === "killed" || status === "shipped") {
          return `<details class="i-group"><summary class="group-label">${label} <span>${group.length}</span></summary>${rows}</details>`;
        }
        return `<section class="i-group"><div class="group-label">${label} <span>${group.length}</span></div>${rows}</section>`;
      }).join("");
    }

    function emptyState() {
      return `<div class="card i-empty"><h2>No ideas yet</h2>
        <p>Ideas keeps track of the projects you start. Describe a new project idea to Claude in any Hangar session and it
        files it here with a few milestones, each with a clear "done when". You can also add one yourself with <b>+ New idea</b>.</p>
        <p class="muted">Open an idea to see its milestones, and use <b>Work on next milestone</b> to start a session on it.</p></div>`;
    }

    render();
    load();
    const timer = setInterval(() => { if (!document.hidden && !isEditing()) load(); }, POLL_MS);
    return { dispose() { disposed = true; clearInterval(timer); } };
  }

  function newIdeaDialog() {
    const dlg = $("#dialog");
    dlg.innerHTML = `<form method="dialog" id="i-new-form">
      <h2>New idea</h2>
      <label class="field"><span>Title</span><input type="text" id="in-title" maxlength="120" required placeholder="e.g. Garden watering planner"></label>
      <label class="field"><span>Summary <small>(optional)</small></span><textarea id="in-summary" rows="3" maxlength="2000" placeholder="What it is and why it matters, in a sentence or two"></textarea></label>
      <p class="error" id="in-err"></p>
      <div class="dialog-actions"><button type="button" class="btn" id="in-cancel">Cancel</button><button class="btn primary" type="submit">Add idea</button></div>
    </form>`;
    dlg.showModal();
    $("#in-title").focus();
    $("#in-cancel").addEventListener("click", () => dlg.close());
    $("#i-new-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { title: $("#in-title").value, summary: $("#in-summary").value };
      try {
        let r = await api(`${BASE}/projects`, { method: "POST", body });
        if (!r.created && !confirm(`This looks like "${r.project.title}", which you already have. Create a new one anyway?\n\nCancel opens the existing one.`)) {
          dlg.close();
          location.hash = `#/ideas/p/${r.project.id}`;
          return;
        }
        if (!r.created) r = await api(`${BASE}/projects`, { method: "POST", body: { ...body, force: true } });
        dlg.close();
        location.hash = `#/ideas/p/${r.project.id}`;
      } catch (err) { $("#in-err").textContent = err.message; }
    });
  }

  async function connectDialog() {
    const dlg = $("#dialog");
    let info;
    try { info = await api(`${BASE}/connect`); } catch (e) { toast(e.message); return; }
    dlg.innerHTML = `<form method="dialog" class="i-connect">
      <h2>Connect Claude Code</h2>
      <p>Sessions started in Hangar are already connected: Claude can file ideas and mark milestones done.</p>
      <p>To use Ideas from Claude Code started outside Hangar on this machine, run this once in a terminal:</p>
      <pre id="ic-cmd">${esc(info.command)}</pre>
      <p class="muted">The command contains your Ideas token, so treat it like a password. The token only works for
      Ideas, not the rest of Hangar.</p>
      <div class="dialog-actions"><button type="button" class="btn" id="ic-copy">Copy command</button>
        <button class="btn primary" type="submit">Done</button></div>
    </form>`;
    dlg.showModal();
    $("#ic-copy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(info.command); toast("Copied"); }
      catch { getSelection().selectAllChildren($("#ic-cmd")); toast("Press Ctrl+C to copy"); }
    });
  }

  /* ---------------- detail ---------------- */

  function detailView(ref) {
    let disposed = false;
    let p = null;
    H.setMain(`${H.topbar("Idea")}<div class="scroll"><div class="page ideas" id="idea"><p class="muted">Loading…</p></div></div>`);

    async function load() {
      try {
        p = await api(`${BASE}/projects/${encodeURIComponent(ref)}`);
        if (!disposed) render();
      } catch (e) {
        if (!disposed) $("#idea").innerHTML = `<h1>Idea not found</h1><p class="muted">${esc(e.message)}. <a href="#/ideas">Back to ideas</a></p>`;
      }
    }

    // Every change returns the updated project, so one helper saves and re-renders.
    async function save(path, method, body, okMsg) {
      try {
        p = await api(`${BASE}${path}`, { method, body });
        render();
        if (okMsg) toast(okMsg);
        return true;
      } catch (e) { toast(e.message); return false; }
    }
    const patch = (fields, msg) => save(`/projects/${p.id}`, "PATCH", fields, msg);
    const patchMilestone = (id, fields, msg) => save(`/milestones/${id}`, "PATCH", fields, msg);

    function sessionTitle(id) {
      const s = H.state.sessions.find((x) => x.id === id);
      return s ? s.title : `Session ${id.slice(0, 8)}`;
    }

    function milestoneRow(m, i, list) {
      const done = m.status === "done";
      const dropped = m.status === "dropped";
      return `<li class="i-ms ${m.status}" data-id="${m.id}">
        <input type="checkbox" class="i-check" data-do="toggle" ${done ? "checked" : ""} ${dropped ? "disabled" : ""} aria-label="Done: ${esc(m.title)}">
        <div class="i-ms-body">
          <div class="i-ms-title">${esc(m.title)} ${m.status === "doing" || dropped ? `<span class="badge">${M_LABEL[m.status]}</span>` : ""}</div>
          ${m.done_criteria ? `<div class="i-ms-done"><span class="dim">Done when:</span> ${esc(m.done_criteria)}</div>` : ""}
          ${m.note ? `<div class="i-ms-note">${esc(m.note)}</div>` : ""}
        </div>
        <div class="i-ms-actions">
          <button class="icon-btn" data-do="up" ${i === 0 ? "disabled" : ""} aria-label="Move up" title="Move up">↑</button>
          <button class="icon-btn" data-do="down" ${i === list.length - 1 ? "disabled" : ""} aria-label="Move down" title="Move down">↓</button>
          <select data-do="status" aria-label="Status of ${esc(m.title)}">
            ${Object.entries(M_LABEL).map(([v, l]) => `<option value="${v}" ${v === m.status ? "selected" : ""}>${l}</option>`).join("")}
          </select>
          <button class="icon-btn" data-do="edit" aria-label="Edit milestone" title="Edit">✎</button>
        </div></li>`;
    }

    function render() {
      const el = $("#idea");
      if (!el || !p) return;
      const tb = $(".topbar .t");
      if (tb) tb.textContent = p.title;
      const ms = p.milestones;
      const killed = p.status === "killed";
      const meta = [
        p.repo_path ? `<span title="${esc(p.repo_path)}">Folder: ${esc(p.repo_path.replace(/^.*\/([^/]+\/[^/]+)$/, "…/$1"))}</span>` : `<span>No folder yet</span>`,
        `<span>Created ${esc(fmt.ago(p.created_at))}</span>`,
        `<span>Updated ${esc(fmt.ago(p.last_activity_at))}</span>`,
      ].join("");
      el.innerHTML = `
        <div class="i-head">
          <div class="i-head-main">
            <a href="#/ideas" class="dim i-back">← Ideas</a>
            <h1><button id="i-rename" title="Rename">${esc(p.title)}</button></h1>
            <div class="s-meta">${meta}</div>
            <div class="i-tags">${tagChips(p.tags)}<button class="btn small ghost" id="i-tags">${p.tags.length ? "Edit tags" : "+ Tags"}</button></div>
          </div>
          <div class="i-head-actions">
            <select id="i-status" aria-label="Project status">${STATUSES.map(([v]) => `<option value="${v}" ${v === p.status ? "selected" : ""}>${STATUS_LABEL[v]}</option>`).join("")}</select>
            <button class="btn primary" id="i-start" ${killed ? "disabled" : ""}>▶ Work on next milestone</button>
            <button class="btn small" id="i-folder">${p.repo_path ? "Change folder" : "Set folder"}</button>
            ${killed ? `<button class="btn small" id="i-revive">Revive</button>` : `<button class="btn small danger" id="i-kill">Discard</button>`}
          </div>
        </div>

        <div class="i-grid">
          <section class="card i-span-2">
            <h2><span>Milestones</span>${progress(p)}</h2>
            ${ms.length ? `<ol class="i-ms-list" id="i-ms">${ms.map(milestoneRow).join("")}</ol>` : `<p class="muted">No milestones yet. Add the first step below.</p>`}
            <form class="i-add" id="i-add-ms">
              <input type="text" id="i-ms-title" maxlength="120" placeholder="New milestone (an outcome)" aria-label="New milestone title" required>
              <input type="text" id="i-ms-done" maxlength="500" placeholder="Done when…" aria-label="Done when">
              <button class="btn small" type="submit">Add</button>
            </form>
          </section>

          <section class="card">
            <h2><span>Summary</span><button class="btn small ghost" id="i-edit-summary">Edit</button></h2>
            <div class="i-md" id="i-summary">${p.summary ? md(p.summary) : `<p class="muted">No summary.</p>`}</div>
          </section>

          <section class="card">
            <h2>Notes</h2>
            <form class="i-note-form" id="i-note-form">
              <textarea id="i-note" rows="2" maxlength="5000" placeholder="Add a note (Markdown)" aria-label="New note"></textarea>
              <div><button class="btn small primary" type="submit">Add note</button></div>
            </form>
            ${p.notes.map((n) => `<div class="i-note"><div class="dim">${esc(fmt.ago(n.created_at))}</div><div class="i-md">${md(n.text)}</div></div>`).join("")}
          </section>

          <section class="card">
            <h2>Sessions</h2>
            ${p.sessions.length ? `<div class="list">${p.sessions.map((s) => `<a class="i-session" href="#/s/${esc(s.session_id)}">
              <span>${esc(sessionTitle(s.session_id))}</span><span class="dim">${esc(fmt.ago(s.first_seen_at))}</span></a>`).join("")}</div>`
              : `<p class="muted">No sessions yet.</p>`}
          </section>

          <section class="card">
            <details><summary class="i-summary-h">Activity</summary>
              <ul class="i-events">${p.events.map((e) => `<li><span>${esc(eventText(e))}</span><span class="dim">${esc(fmt.ago(e.created_at))}</span></li>`).join("")}</ul>
            </details>
          </section>
        </div>`;
      wire();
    }

    function eventText(e) {
      const d = e.detail || {};
      switch (e.type) {
        case "created": return `Created${d.by === "claude" ? " by Claude" : ""}`;
        case "milestone_added": return `Added ${d.count} milestone${d.count === 1 ? "" : "s"}${d.by === "claude" ? " (Claude)" : ""}`;
        case "milestone_updated": return `Milestone "${d.title}"${d.status ? ` → ${M_LABEL[d.status] || d.status}` : " updated"}`;
        case "status_changed": return `Status: ${STATUS_LABEL[d.from] || d.from} → ${STATUS_LABEL[d.to] || d.to}`;
        case "session_linked": return "Session linked";
        default: return e.type;
      }
    }

    function wire() {
      $("#i-rename").addEventListener("click", () => {
        const title = prompt("Project title:", p.title);
        if (title && title.trim() && title.trim() !== p.title) patch({ title: title.trim() });
      });
      $("#i-tags").addEventListener("click", () => {
        const v = prompt("Tags, separated by commas:", p.tags.join(", "));
        if (v === null) return;
        patch({ tags: v.split(",").map((t) => t.trim()).filter(Boolean) });
      });
      $("#i-status").addEventListener("change", (e) => patch({ status: e.target.value }, `Moved to ${STATUS_LABEL[e.target.value]}`));
      $("#i-kill")?.addEventListener("click", () => {
        const why = prompt("Discard this idea? It moves to Killed (you can revive it later).\n\nWhy? (optional)", "");
        if (why === null) return;
        patch(why.trim() ? { status: "killed", note: why.trim() } : { status: "killed" }, "Discarded");
      });
      $("#i-revive")?.addEventListener("click", () => patch({ status: "idea" }, "Revived"));
      $("#i-start").addEventListener("click", () => startWork());
      $("#i-folder").addEventListener("click", async () => {
        const folder = await pickFolder(p.repo_path, "Project folder");
        if (folder) patch({ repo_path: folder.path }, "Folder saved");
      });
      $("#i-edit-summary").addEventListener("click", () => {
        const box = $("#i-summary");
        box.innerHTML = `<textarea id="i-summary-text" rows="5" maxlength="2000">${esc(p.summary)}</textarea>
          <div class="i-row-btns"><button class="btn small" id="i-sum-cancel">Cancel</button><button class="btn small primary" id="i-sum-save">Save</button></div>`;
        $("#i-summary-text").focus();
        $("#i-sum-cancel").addEventListener("click", render);
        $("#i-sum-save").addEventListener("click", () => patch({ summary: $("#i-summary-text").value }));
      });
      $("#i-add-ms").addEventListener("submit", (e) => {
        e.preventDefault();
        const title = $("#i-ms-title").value.trim();
        if (!title) return;
        save(`/projects/${p.id}/milestones`, "POST", { milestones: [{ title, done_criteria: $("#i-ms-done").value }] })
          .then((ok) => ok && $("#i-ms-title")?.focus());
      });
      $("#i-note-form").addEventListener("submit", (e) => {
        e.preventDefault();
        const text = $("#i-note").value.trim();
        if (text) save(`/projects/${p.id}/notes`, "POST", { text });
      });
      const list = $("#i-ms");
      if (!list) return;
      list.addEventListener("click", (e) => {
        const b = e.target.closest("[data-do]");
        if (!b || b.tagName === "SELECT") return;
        const li = b.closest("[data-id]");
        const id = Number(li.dataset.id);
        const idx = p.milestones.findIndex((m) => m.id === id);
        const m = p.milestones[idx];
        if (b.dataset.do === "toggle") patchMilestone(id, { status: b.checked ? "done" : "todo" });
        else if (b.dataset.do === "up") patchMilestone(id, { position: idx });
        else if (b.dataset.do === "down") patchMilestone(id, { position: idx + 2 });
        else if (b.dataset.do === "edit") editMilestone(m);
      });
      list.addEventListener("change", (e) => {
        if (e.target.dataset.do !== "status") return;
        patchMilestone(Number(e.target.closest("[data-id]").dataset.id), { status: e.target.value });
      });
    }

    function editMilestone(m) {
      const dlg = $("#dialog");
      dlg.innerHTML = `<form method="dialog" id="i-ms-form">
        <h2>Edit milestone</h2>
        <label class="field"><span>Title</span><input type="text" id="em-title" maxlength="120" required value="${esc(m.title)}"></label>
        <label class="field"><span>Done when</span><textarea id="em-done" rows="2" maxlength="500">${esc(m.done_criteria)}</textarea></label>
        <label class="field"><span>Note</span><textarea id="em-note" rows="2" maxlength="2000">${esc(m.note || "")}</textarea></label>
        <div class="dialog-actions"><button type="button" class="btn" id="em-cancel">Cancel</button><button class="btn primary" type="submit">Save</button></div>
      </form>`;
      dlg.showModal();
      $("#em-cancel").addEventListener("click", () => dlg.close());
      $("#i-ms-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const ok = await patchMilestone(m.id, { title: $("#em-title").value, done_criteria: $("#em-done").value, note: $("#em-note").value });
        if (ok) dlg.close();
      });
    }

    async function startWork(cwd, saveFolder) {
      const btn = $("#i-start");
      if (btn) btn.disabled = true;
      try {
        const r = await api(`${BASE}/projects/${p.id}/start-session`, { method: "POST", body: cwd ? { cwd, save_folder: saveFolder } : {} });
        toast("Session started");
        await H.loadSessions();
        location.hash = r.claude_id ? `#/s/${r.claude_id}` : `#/t/${r.name}`;
      } catch (e) {
        if (btn) btn.disabled = false;
        if (!cwd && /no folder/i.test(e.message)) {
          const folder = await pickFolder("", "Where should Claude work on this?", true);
          if (folder) startWork(folder.path, folder.remember);
        } else toast(e.message);
      }
    }

    load();
    const timer = setInterval(() => { if (!document.hidden && !isEditing(true)) load(); }, POLL_MS);
    return { dispose() { disposed = true; clearInterval(timer); } };
  }

  /* Folder picker over /api/dirs (limited to HANGAR_WORKSPACES). Resolves to {path, remember} or null. */
  function pickFolder(start, heading, offerRemember = false) {
    return new Promise((resolve) => {
      const dlg = $("#dialog");
      let cwd = "";
      dlg.innerHTML = `<form method="dialog" id="i-pick">
        <h2>${esc(heading)}</h2>
        <div class="picker"><div class="picker-path"><button type="button" class="btn small ghost" id="ip-up" aria-label="Up one folder">↑</button><span id="ip-path"></span></div>
          <div class="picker-list" id="ip-list"></div></div>
        ${offerRemember ? `<label class="check"><input type="checkbox" id="ip-remember" checked> Use this folder for the project from now on</label>` : ""}
        <p class="error" id="ip-err"></p>
        <div class="dialog-actions"><button type="button" class="btn" id="ip-cancel">Cancel</button><button class="btn primary" type="submit" id="ip-go">Use this folder</button></div>
      </form>`;
      dlg.showModal();
      let result = null;
      dlg.addEventListener("close", () => resolve(result), { once: true });

      async function browse(path) {
        try {
          const d = await api("/api/dirs?path=" + encodeURIComponent(path || ""));
          cwd = d.path;
          $("#ip-path").textContent = d.path ? "‎" + d.path : "Workspaces";
          $("#ip-go").disabled = !d.path;
          $("#ip-up").disabled = !d.parent && !d.path;
          $("#ip-up").onclick = () => browse(d.parent || "");
          $("#ip-list").innerHTML = d.dirs.map((x) => `<button type="button" data-path="${esc(x.path)}">${esc(x.name)}</button>`).join("") || `<div class="empty">No subfolders</div>`;
        } catch (e) {
          $("#ip-err").textContent = e.message;
          if (path) browse("");
        }
      }
      $("#ip-list").addEventListener("click", (e) => { const b = e.target.closest("[data-path]"); if (b) browse(b.dataset.path); });
      $("#ip-cancel").addEventListener("click", () => dlg.close());
      $("#i-pick").addEventListener("submit", (e) => {
        e.preventDefault();
        if (!cwd) return;
        result = { path: cwd, remember: offerRemember ? $("#ip-remember").checked : true };
        dlg.close();
      });
      browse(start || "");
    });
  }

  H.registerRoute(/^#\/ideas\/?$/, () => listView());
  H.registerRoute(/^#\/ideas\/p\/([\w-]+)$/, (m) => detailView(m[1]));
})();
