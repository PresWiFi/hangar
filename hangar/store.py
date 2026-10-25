"""Hangar's own small SQLite database: session labels, pins, notes, and app state."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from .config import settings

_lock = threading.Lock()
_db = sqlite3.connect(settings.data_dir / "hangar.db", check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript("""
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (
    session_id TEXT PRIMARY KEY,
    name TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    updated REAL NOT NULL
);
""")
_db.commit()

META_FIELDS = {"name", "pinned", "archived", "notes"}


def kv_get(key: str, default=None):
    with _lock:
        row = _db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(key: str, value) -> None:
    with _lock:
        _db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)))
        _db.commit()


def all_meta() -> dict[str, dict]:
    with _lock:
        rows = _db.execute("SELECT * FROM meta").fetchall()
    return {r["session_id"]: dict(r) for r in rows}


def set_meta(session_id: str, **fields) -> dict:
    fields = {k: v for k, v in fields.items() if k in META_FIELDS}
    with _lock:
        _db.execute("INSERT OR IGNORE INTO meta (session_id, updated) VALUES (?, ?)", (session_id, time.time()))
        for key, value in fields.items():
            if key in {"pinned", "archived"}:
                value = 1 if value else 0
            _db.execute(f"UPDATE meta SET {key} = ?, updated = ? WHERE session_id = ?", (value, time.time(), session_id))
        _db.commit()
        row = _db.execute("SELECT * FROM meta WHERE session_id = ?", (session_id,)).fetchone()
    return dict(row)
