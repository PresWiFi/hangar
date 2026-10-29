"""The Ideas database: its own SQLite file (ideas.db in the data dir), separate from Hangar's hangar.db."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from ..config import settings

# Each entry upgrades the schema by one version. Append new entries; never edit old ones.
MIGRATIONS = [
    """
    CREATE TABLE ideas_projects (
        id INTEGER PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'idea'
            CHECK (status IN ('idea', 'planned', 'active', 'paused', 'shipped', 'killed')),
        repo_path TEXT,
        tags TEXT NOT NULL DEFAULT '[]',
        kill_criteria TEXT,
        source_session_id TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_activity_at REAL NOT NULL
    );
    CREATE TABLE ideas_milestones (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES ideas_projects(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        title TEXT NOT NULL,
        done_criteria TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'doing', 'done', 'dropped')),
        created_by TEXT NOT NULL DEFAULT 'user' CHECK (created_by IN ('claude', 'user')),
        completed_at REAL,
        note TEXT
    );
    CREATE INDEX ideas_milestones_project ON ideas_milestones (project_id, position);
    CREATE TABLE ideas_notes (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES ideas_projects(id) ON DELETE CASCADE,
        text TEXT NOT NULL,
        session_id TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE ideas_session_links (
        project_id INTEGER NOT NULL REFERENCES ideas_projects(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        first_seen_at REAL NOT NULL,
        PRIMARY KEY (project_id, session_id)
    );
    CREATE TABLE ideas_events (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES ideas_projects(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        detail TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX ideas_events_project ON ideas_events (project_id, created_at);
    """,
]

lock = threading.RLock()  # wrap every use of the connection, as hangar/store.py does
_conn: sqlite3.Connection | None = None


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """Open (or reopen) the database and bring its schema up to date. Tests pass their own path."""
    global _conn
    with lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(path or settings.data_dir / "ideas.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        migrate(_conn)
        return _conn


def conn() -> sqlite3.Connection:
    with lock:
        return _conn if _conn is not None else open_db()


def schema_version(c: sqlite3.Connection) -> int:
    c.execute("CREATE TABLE IF NOT EXISTS ideas_schema_version (version INTEGER NOT NULL)")
    row = c.execute("SELECT version FROM ideas_schema_version").fetchone()
    return row[0] if row else 0


def migrate(c: sqlite3.Connection) -> None:
    current = schema_version(c)
    c.commit()
    if current > len(MIGRATIONS):
        raise RuntimeError(f"ideas.db is schema version {current}, newer than this Hangar ({len(MIGRATIONS)})")
    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        # One transaction per step: a failed step leaves the database at the previous version.
        script = (f"BEGIN;\n{sql}\nDELETE FROM ideas_schema_version;\n"
                  f"INSERT INTO ideas_schema_version (version) VALUES ({version});\nCOMMIT;")
        try:
            c.executescript(script)
        except sqlite3.Error:
            c.rollback()
            raise
