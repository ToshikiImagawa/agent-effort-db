from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from effort_db import config
from effort_db.cli import app

runner = CliRunner()


def test_init_creates_db(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "effort.db"
    monkeypatch.setenv(config.ENV_DB_PATH, str(db_path))

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"sessions", "pull_requests"} <= tables
    finally:
        conn.close()


def test_init_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "effort.db"
    monkeypatch.setenv(config.ENV_DB_PATH, str(db_path))

    first = runner.invoke(app, ["init"])
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, issue_key, turns) VALUES (?, ?, ?)",
            ("session-1", "SAMPLE-1", 3),
        )
        conn.commit()
    finally:
        conn.close()

    second = runner.invoke(app, ["init"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT turns FROM sessions WHERE session_id = ?", ("session-1",)
        ).fetchone()
        assert row == (3,)
    finally:
        conn.close()


def test_unimplemented_commands_report_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_DB_PATH, str(tmp_path / "effort.db"))

    result = runner.invoke(app, ["stats"])

    assert result.exit_code == 1
    assert "未実装" in result.output
