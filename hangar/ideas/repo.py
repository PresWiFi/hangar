"""Ideas data access: projects, milestones, notes, session links, and the activity log.

All functions are synchronous (the API runs them in a thread) and hold db.lock for their whole run,
so each call is one consistent unit.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time

from .. import files
from . import db, similar

STATUSES = ("idea", "planned", "active", "paused", "shipped", "killed")
MILESTONE_STATUSES = ("todo", "doing", "done", "dropped")
MAX_MILESTONES_PER_PROJECT = 50


class NotFound(Exception):
    pass


class Invalid(Exception):
    pass


# ---------- helpers ----------

def _project(row: sqlite3.Row) -> dict:
    p = dict(row)
    p["tags"] = json.loads(p["tags"] or "[]")
    return p


def _progress(c: sqlite3.Connection, pid: int) -> dict:
    rows = c.execute("SELECT status, COUNT(*) AS n FROM ideas_milestones WHERE project_id = ? GROUP BY status",
                     (pid,)).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    return {"done": counts.get("done", 0), "total": sum(counts.values()) - counts.get("dropped", 0)}


def _next_milestone(c: sqlite3.Connection, pid: int) -> dict | None:
    row = c.execute("SELECT * FROM ideas_milestones WHERE project_id = ? AND status IN ('doing', 'todo') "
                    "ORDER BY status = 'doing' DESC, position LIMIT 1", (pid,)).fetchone()
    return dict(row) if row else None


def _summary(c: sqlite3.Connection, row: sqlite3.Row) -> dict:
    p = _project(row)
    p["progress"] = _progress(c, p["id"])
    p["next_milestone"] = _next_milestone(c, p["id"])
    return p


def _event(c: sqlite3.Connection, pid: int, kind: str, detail: dict | None = None) -> None:
    c.execute("INSERT INTO ideas_events (project_id, type, detail, created_at) VALUES (?, ?, ?, ?)",
              (pid, kind, json.dumps(detail) if detail else None, time.time()))


def _touch(c: sqlite3.Connection, pid: int, session_id: str | None) -> None:
    """Record activity on a project, and link the session that caused it."""
    now = time.time()
    c.execute("UPDATE ideas_projects SET last_activity_at = ?, updated_at = ? WHERE id = ?", (now, now, pid))
    if session_id:
        cur = c.execute("INSERT OR IGNORE INTO ideas_session_links (project_id, session_id, first_seen_at) "
                        "VALUES (?, ?, ?)", (pid, session_id, now))
        if cur.rowcount:
            _event(c, pid, "session_linked", {"session_id": session_id})


def _slug(c: sqlite3.Connection, title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-") or "project"
    slug, i = base, 2
    while c.execute("SELECT 1 FROM ideas_projects WHERE slug = ?", (slug,)).fetchone():
        slug, i = f"{base}-{i}", i + 1
    return slug


def clean_repo_path(path: str | None) -> str | None:
    """Empty means none. Otherwise it must be an absolute path inside HANGAR_WORKSPACES."""
    if not path:
        return None
    if not os.path.isabs(path):
        raise Invalid("repo_path must be an absolute path")
    if not files.inside_workspace(path):
        raise Invalid("repo_path is outside the configured workspaces (HANGAR_WORKSPACES)")
    return os.path.realpath(path)


def _insert_milestones(c: sqlite3.Connection, pid: int, items: list[dict], created_by: str) -> list[int]:
    count = c.execute("SELECT COUNT(*) FROM ideas_milestones WHERE project_id = ?", (pid,)).fetchone()[0]
    if count + len(items) > MAX_MILESTONES_PER_PROJECT:
        raise Invalid(f"A project can have at most {MAX_MILESTONES_PER_PROJECT} milestones")
    top = c.execute("SELECT COALESCE(MAX(position), 0) FROM ideas_milestones WHERE project_id = ?",
                    (pid,)).fetchone()[0]
    ids = []
    for i, m in enumerate(items, start=1):
        cur = c.execute("INSERT INTO ideas_milestones (project_id, position, title, done_criteria, created_by) "
                        "VALUES (?, ?, ?, ?, ?)", (pid, top + i, m["title"], m.get("done_criteria") or "", created_by))
        ids.append(cur.lastrowid)
    return ids


# ---------- reads ----------

def resolve(ref: str | int) -> int:
    """Project id from a numeric id or a slug."""
    c = db.conn()
    with db.lock:
        ref = str(ref)
        row = c.execute("SELECT id FROM ideas_projects WHERE " + ("id = ?" if ref.isdigit() else "slug = ?"),
                        (int(ref) if ref.isdigit() else ref,)).fetchone()
    if not row:
        raise NotFound("Project not found")
    return row["id"]


def _search_score(query: str, row: sqlite3.Row) -> float:
    title_score = similar.score(query, row["title"])
    q = similar.tokens(query)
    if not q:
        return title_score
    words = similar.tokens(" ".join([row["title"], row["summary"], " ".join(json.loads(row["tags"] or "[]"))]))
    hits = sum(1 for t in q if any(similar.same(t, w) for w in words))
    return max(title_score, hits / len(q))


def list_projects(status: str | None = None, q: str | None = None, limit: int = 200) -> list[dict]:
    if status and status not in STATUSES:
        raise Invalid(f"Unknown status: {status}")
    c = db.conn()
    with db.lock:
        sql = "SELECT * FROM ideas_projects" + (" WHERE status = ?" if status else "") + " ORDER BY last_activity_at DESC"
        rows = c.execute(sql, (status,) if status else ()).fetchall()
        if q and q.strip():
            scored = [(s, r) for r in rows if (s := _search_score(q, r)) > 0]
            scored.sort(key=lambda x: (-x[0], -x[1]["last_activity_at"]))
            out = []
            for s, r in scored[:limit]:
                p = _summary(c, r)
                p["score"] = round(s, 2)
                out.append(p)
            return out
        return [_summary(c, r) for r in rows[:limit]]


def get_project(pid: int) -> dict:
    c = db.conn()
    with db.lock:
        row = c.execute("SELECT * FROM ideas_projects WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise NotFound("Project not found")
        p = _summary(c, row)
        p["milestones"] = [dict(r) for r in c.execute(
            "SELECT * FROM ideas_milestones WHERE project_id = ? ORDER BY position", (pid,))]
        p["notes"] = [dict(r) for r in c.execute(
            "SELECT * FROM ideas_notes WHERE project_id = ? ORDER BY created_at DESC, id DESC", (pid,))]
        p["sessions"] = [dict(r) for r in c.execute(
            "SELECT session_id, first_seen_at FROM ideas_session_links WHERE project_id = ? ORDER BY first_seen_at",
            (pid,))]
        p["events"] = [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in c.execute(
            "SELECT * FROM ideas_events WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT 50", (pid,))]
        return p


def find_duplicate(title: str) -> tuple[dict, float] | None:
    """The most similar existing project that isn't killed, if it looks like the same idea."""
    c = db.conn()
    with db.lock:
        best = None
        for row in c.execute("SELECT id, title FROM ideas_projects WHERE status != 'killed'"):
            s = similar.score(title, row["title"])
            if s >= similar.THRESHOLD and (best is None or s > best[1]):
                best = (row["id"], s)
    return (get_project(best[0]), best[1]) if best else None


# ---------- writes ----------

def create_project(*, title: str, summary: str = "", milestones: list[dict] | None = None,
                   tags: list[str] | None = None, repo_path: str | None = None, status: str = "idea",
                   actor: str = "user", session_id: str | None = None, force: bool = False) -> dict:
    """Returns {"created": bool, "project": {...}}. A likely duplicate is returned instead of created,
    unless force is set."""
    if status not in STATUSES:
        raise Invalid(f"Unknown status: {status}")
    repo_path = clean_repo_path(repo_path)
    c = db.conn()
    with db.lock:
        if not force:
            dup = find_duplicate(title)
            if dup:
                project, score = dup
                return {"created": False, "reason": "duplicate", "score": round(score, 2), "project": project}
        now = time.time()
        try:
            cur = c.execute(
                "INSERT INTO ideas_projects (slug, title, summary, status, repo_path, tags, source_session_id, "
                "created_at, updated_at, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_slug(c, title), title, summary, status, repo_path, json.dumps(tags or []), session_id, now, now, now))
            pid = cur.lastrowid
            _event(c, pid, "created", {"by": actor})
            if milestones:
                _insert_milestones(c, pid, milestones, actor)
                _event(c, pid, "milestone_added", {"count": len(milestones), "by": actor})
            _touch(c, pid, session_id)
            c.commit()
        except Exception:
            c.rollback()
            raise
        return {"created": True, "project": get_project(pid)}


def update_project(pid: int, fields: dict, session_id: str | None = None) -> dict:
    allowed = {"title", "summary", "status", "repo_path", "tags", "kill_criteria"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "status" in fields and fields["status"] not in STATUSES:
        raise Invalid(f"Unknown status: {fields['status']}")
    if "repo_path" in fields:
        fields["repo_path"] = clean_repo_path(fields["repo_path"])
    if "tags" in fields:
        fields["tags"] = json.dumps(fields["tags"] or [])
    c = db.conn()
    with db.lock:
        old = c.execute("SELECT * FROM ideas_projects WHERE id = ?", (pid,)).fetchone()
        if not old:
            raise NotFound("Project not found")
        try:
            for key, value in fields.items():
                c.execute(f"UPDATE ideas_projects SET {key} = ? WHERE id = ?", (value, pid))
            if "status" in fields and fields["status"] != old["status"]:
                _event(c, pid, "status_changed", {"from": old["status"], "to": fields["status"]})
            _touch(c, pid, session_id)
            c.commit()
        except Exception:
            c.rollback()
            raise
    return get_project(pid)


def set_status(pid: int, status: str, note: str | None = None, session_id: str | None = None) -> dict:
    """Change status and, optionally, record why as a note, in one step (used by the MCP tool)."""
    project = update_project(pid, {"status": status}, session_id)
    return add_note(pid, note, session_id) if note else project


def add_milestones(pid: int, items: list[dict], actor: str = "user", session_id: str | None = None) -> dict:
    c = db.conn()
    with db.lock:
        if not c.execute("SELECT 1 FROM ideas_projects WHERE id = ?", (pid,)).fetchone():
            raise NotFound("Project not found")
        try:
            _insert_milestones(c, pid, items, actor)
            _event(c, pid, "milestone_added", {"count": len(items), "by": actor})
            _touch(c, pid, session_id)
            c.commit()
        except Exception:
            c.rollback()
            raise
    return get_project(pid)


def update_milestone(mid: int, fields: dict, session_id: str | None = None) -> dict:
    """Change a milestone's status, note, title, done criteria, or position. Returns its project."""
    allowed = {"status", "note", "title", "done_criteria", "position"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "status" in fields and fields["status"] not in MILESTONE_STATUSES:
        raise Invalid(f"Unknown milestone status: {fields['status']}")
    c = db.conn()
    with db.lock:
        m = c.execute("SELECT * FROM ideas_milestones WHERE id = ?", (mid,)).fetchone()
        if not m:
            raise NotFound("Milestone not found")
        pid = m["project_id"]
        try:
            if "position" in fields:
                _move(c, pid, mid, int(fields.pop("position")))
            if "status" in fields:
                if fields["status"] != "done":
                    completed = None
                else:  # keep the original time if it was already done
                    completed = m["completed_at"] if m["status"] == "done" else time.time()
                c.execute("UPDATE ideas_milestones SET completed_at = ? WHERE id = ?", (completed, mid))
            for key, value in fields.items():
                c.execute(f"UPDATE ideas_milestones SET {key} = ? WHERE id = ?", (value, mid))
            changes = {k: v for k, v in fields.items() if k != "note" and m[k] != v}
            if changes or "note" in fields:
                _event(c, pid, "milestone_updated", {"milestone_id": mid, "title": m["title"], **changes})
            _touch(c, pid, session_id)
            c.commit()
        except Exception:
            c.rollback()
            raise
    return get_project(pid)


def _move(c: sqlite3.Connection, pid: int, mid: int, position: int) -> None:
    """Put a milestone at a 1-based position and renumber the rest."""
    ids = [r["id"] for r in c.execute("SELECT id FROM ideas_milestones WHERE project_id = ? ORDER BY position", (pid,))]
    ids.remove(mid)
    ids.insert(max(0, min(position - 1, len(ids))), mid)
    for i, x in enumerate(ids, start=1):
        c.execute("UPDATE ideas_milestones SET position = ? WHERE id = ?", (i, x))


def add_note(pid: int, text: str, session_id: str | None = None) -> dict:
    c = db.conn()
    with db.lock:
        if not c.execute("SELECT 1 FROM ideas_projects WHERE id = ?", (pid,)).fetchone():
            raise NotFound("Project not found")
        try:
            c.execute("INSERT INTO ideas_notes (project_id, text, session_id, created_at) VALUES (?, ?, ?, ?)",
                      (pid, text, session_id, time.time()))
            _touch(c, pid, session_id)
            c.commit()
        except Exception:
            c.rollback()
            raise
    return get_project(pid)


# ---------- starting work ----------

def opening_prompt(project: dict) -> str:
    """First message for a session started from a project: what it is and what to do next."""
    lines = [f'I\'d like to work on my project "{project["title"]}" (tracked in Hangar Ideas as `{project["slug"]}`).']
    if project["summary"]:
        lines += ["", f"Summary: {project['summary']}"]
    m = project.get("next_milestone")
    if m:
        total = project["progress"]["total"]
        lines += ["", f"Next milestone ({project['progress']['done'] + 1} of {total}): {m['title']}"]
        if m["done_criteria"]:
            lines.append(f"Done when: {m['done_criteria']}")
        lines += ["", "Please start on this milestone. If you have the Hangar Ideas tools, mark it done once "
                      "its done-when criterion is met."]
    else:
        lines += ["", "It has no open milestones. Help me decide the next step, and add it as a milestone if you "
                      "have the Hangar Ideas tools."]
    return "\n".join(lines)


def mark_started(pid: int, session_id: str | None) -> dict:
    """A session was started on a project: link it, move the next milestone to doing, and make the project active."""
    project = get_project(pid)
    m = project.get("next_milestone")
    if m and m["status"] == "todo":
        update_milestone(m["id"], {"status": "doing"}, session_id)
    if project["status"] in ("idea", "planned", "paused"):
        update_project(pid, {"status": "active"}, session_id)
    elif session_id:
        update_project(pid, {}, session_id)
    return get_project(pid)
