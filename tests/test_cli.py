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

    result = runner.invoke(app, ["query", "SELECT 1"])

    assert result.exit_code == 1
    assert "未実装" in result.output


def _init_db(tmp_path: Path, monkeypatch, *, with_patterns: bool = True) -> Path:
    """stats / relink 用にDBを用意する。config.toml は tmp 側に隔離する。

    実装は既定パターンを持たないため、既定では架空のパターンを設定して起動する。
    """
    db_path = tmp_path / "effort.db"
    data_dir = tmp_path / "data"
    monkeypatch.setenv(config.ENV_DB_PATH, str(db_path))
    monkeypatch.setattr(config, "DEFAULT_DATA_DIR", data_dir)
    if with_patterns:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / config.CONFIG_FILE_NAME).write_text(
            'issue_key_patterns = ["SAMPLE-[0-9]+"]\n', encoding="utf-8"
        )
    assert runner.invoke(app, ["init"]).exit_code == 0
    return db_path


def test_stats_on_empty_db_shows_zero_percent(tmp_path: Path, monkeypatch) -> None:
    _init_db(tmp_path, monkeypatch)

    result = runner.invoke(app, ["stats"])

    assert result.exit_code == 0
    assert "0 行, issue_key 有り 0 行 (0.0%)" in result.output
    assert "[警告]" not in result.output


def test_stats_warns_when_patterns_are_not_configured(tmp_path: Path, monkeypatch) -> None:
    _init_db(tmp_path, monkeypatch, with_patterns=False)

    result = runner.invoke(app, ["stats"])

    assert result.exit_code == 0
    assert "[警告]" in result.output
    assert config.CONFIG_KEY_ISSUE_KEY_PATTERNS in result.output


def test_stats_reports_join_rate_and_warns_when_low(tmp_path: Path, monkeypatch) -> None:
    db_path = _init_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO sessions (session_id, branch, issue_key) VALUES (?, ?, ?)",
            [
                ("s-1", "feat/SAMPLE-1", "SAMPLE-1"),
                ("s-2", "feat/no-key", None),
                ("s-3", None, None),
            ],
        )
        conn.execute(
            "INSERT INTO pull_requests (repo, pr_number, issue_key) VALUES (?, ?, ?)",
            ("example/repo", 1, "SAMPLE-1"),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["stats"])

    assert result.exit_code == 0
    assert "3 行, issue_key 有り 1 行 (33.3%)" in result.output
    assert "join 済み     : 1 issue_key" in result.output
    assert "[警告] sessions の issue_key 抽出率が低い" in result.output


def test_stats_without_init_reports_guidance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_DB_PATH, str(tmp_path / "missing.db"))
    monkeypatch.setattr(config, "DEFAULT_DATA_DIR", tmp_path / "data")

    result = runner.invoke(app, ["stats"])

    assert result.exit_code == 1
    assert "effort-db init" in result.output


def test_relink_fills_issue_key_from_branch(tmp_path: Path, monkeypatch) -> None:
    db_path = _init_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO sessions (session_id, branch) VALUES (?, ?)",
            [("s-1", "feat/SAMPLE-1-add"), ("s-2", None)],
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["relink"])

    assert result.exit_code == 0
    conn = sqlite3.connect(db_path)
    try:
        rows = dict(conn.execute("SELECT session_id, issue_key FROM sessions").fetchall())
        assert rows == {"s-1": "SAMPLE-1", "s-2": None}
    finally:
        conn.close()


def test_relink_without_patterns_does_nothing(tmp_path: Path, monkeypatch) -> None:
    db_path = _init_db(tmp_path, monkeypatch, with_patterns=False)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO sessions (session_id, branch) VALUES ('s-1', 'feat/SAMPLE-1')")
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["relink"])

    assert result.exit_code == 0
    assert config.CONFIG_KEY_ISSUE_KEY_PATTERNS in result.output
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT issue_key FROM sessions").fetchone() == (None,)
    finally:
        conn.close()
