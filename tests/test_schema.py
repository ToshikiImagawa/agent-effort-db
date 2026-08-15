from __future__ import annotations

import sqlite3
from pathlib import Path

from effort_db import schema


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def _view_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall()
    return {row[0] for row in rows}


def test_init_db_creates_tables_and_view(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "effort.db")
    try:
        schema.init_db(conn)

        assert {"sessions", "pull_requests"} <= _table_names(conn)
        assert "task_effort" in _view_names(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    finally:
        conn.close()


def test_init_db_is_idempotent_and_preserves_data(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "effort.db")
    try:
        schema.init_db(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, issue_key, turns) VALUES (?, ?, ?)",
            ("session-1", "SAMPLE-1", 5),
        )
        conn.commit()

        schema.init_db(conn)

        row = conn.execute(
            "SELECT issue_key, turns FROM sessions WHERE session_id = ?", ("session-1",)
        ).fetchone()
        assert row == ("SAMPLE-1", 5)
    finally:
        conn.close()
