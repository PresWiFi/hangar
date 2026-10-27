import sqlite3

import pytest

from hangar.ideas import db, repo


def _version(path):
    return sqlite3.connect(path).execute("SELECT version FROM ideas_schema_version").fetchone()[0]


def test_fresh_database_is_at_latest_version(tmp_path):
    db.open_db(tmp_path / "a.db")
    assert _version(tmp_path / "a.db") == len(db.MIGRATIONS)


def test_reopening_keeps_data(tmp_path):
    db.open_db(tmp_path / "a.db")
    repo.create_project(title="Keep me")
    db.open_db(tmp_path / "a.db")
    assert _version(tmp_path / "a.db") == len(db.MIGRATIONS)
    assert [p["title"] for p in repo.list_projects()] == ["Keep me"]


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "a.db"
    db.open_db(path)
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + ["CREATE TABLE ideas_half (x INTEGER); THIS IS NOT SQL;"])
    with pytest.raises(sqlite3.Error):
        db.open_db(path)
    c = sqlite3.connect(path)
    assert _version(path) == len(db.MIGRATIONS) - 1
    assert not c.execute("SELECT name FROM sqlite_master WHERE name = 'ideas_half'").fetchone()


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "a.db"
    db.open_db(path)
    c = sqlite3.connect(path)
    c.execute("UPDATE ideas_schema_version SET version = 99")
    c.commit()
    with pytest.raises(RuntimeError, match="newer"):
        db.open_db(path)


def test_foreign_keys_are_enforced(tmp_path):
    db.open_db(tmp_path / "a.db")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn().execute("INSERT INTO ideas_milestones (project_id, position, title) VALUES (999, 1, 'x')")
