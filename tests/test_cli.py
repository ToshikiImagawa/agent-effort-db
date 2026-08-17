from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from effort_db import config
from effort_db.cli import app
from effort_db.collectors import session

runner = CliRunner()

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "projects"


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


def _init_db(tmp_path: Path, monkeypatch, *, with_patterns: bool = True) -> Path:
    """stats / link / query 用にDBを用意する。config.toml は tmp 側に隔離する。

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


def test_stats_on_empty_db_shows_zero_without_warnings(tmp_path: Path, monkeypatch) -> None:
    _init_db(tmp_path, monkeypatch)

    result = runner.invoke(app, ["stats"])

    assert result.exit_code == 0
    assert "sessions      : 0 行" in result.output
    assert "[警告]" not in result.output


def test_stats_warns_when_patterns_are_not_configured(tmp_path: Path, monkeypatch) -> None:
    _init_db(tmp_path, monkeypatch, with_patterns=False)

    result = runner.invoke(app, ["stats"])

    assert result.exit_code == 0
    assert "[警告]" in result.output
    assert config.CONFIG_KEY_ISSUE_KEY_PATTERNS in result.output


def test_stats_reports_join_rate_per_source_and_warns_when_low(tmp_path: Path, monkeypatch) -> None:
    db_path = _init_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO sessions (session_id, repo, branch) VALUES (?, ?, ?)",
            [
                ("s-1", "example/repo", "feat/SAMPLE-1"),
                ("s-2", "example/repo", "feat/no-pr"),
                ("s-3", "example/repo", None),
            ],
        )
        conn.execute(
            "INSERT INTO pull_requests (repo, pr_number, head_branch) VALUES (?, ?, ?)",
            ("example/repo", 1, "feat/SAMPLE-1"),
        )
        conn.commit()
    finally:
        conn.close()
    assert runner.invoke(app, ["link"]).exit_code == 0

    result = runner.invoke(app, ["stats"])

    assert result.exit_code == 0
    assert "repo_branch   : 1 セッション (33.3%)" in result.output
    assert "unlinked      : 2 セッション (66.7%)" in result.output
    assert "[警告] PR 収集済みリポジトリでの join 率が低い" in result.output
    assert "[情報] repo または branch が欠けたセッションが 1 行ある" in result.output


def test_stats_without_init_reports_guidance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_DB_PATH, str(tmp_path / "missing.db"))
    monkeypatch.setattr(config, "DEFAULT_DATA_DIR", tmp_path / "data")

    result = runner.invoke(app, ["stats"])

    assert result.exit_code == 1
    assert "effort-db init" in result.output


def test_link_creates_links_and_assigns_issue_keys(tmp_path: Path, monkeypatch) -> None:
    db_path = _init_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO sessions (session_id, repo, branch) VALUES (?, ?, ?)",
            [("s-1", "example/repo", "feat/SAMPLE-1-add"), ("s-2", "example/repo", None)],
        )
        conn.execute(
            "INSERT INTO pull_requests (repo, pr_number, head_branch) VALUES (?, ?, ?)",
            ("example/repo", 3, "feat/SAMPLE-1-add"),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["link"])

    assert result.exit_code == 0
    assert "repo_branch   : 1 件のリンクを追加" in result.output
    assert "unlinked      : 1 セッション" in result.output
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT session_id, pr_number, link_source FROM session_pr_links"
        ).fetchall() == [("s-1", 3, "repo_branch")]
        rows = dict(conn.execute("SELECT session_id, issue_key FROM sessions").fetchall())
        assert rows == {"s-1": "SAMPLE-1", "s-2": None}
    finally:
        conn.close()


def test_link_without_patterns_still_links_but_warns(tmp_path: Path, monkeypatch) -> None:
    """チケットキーの設定が無くても (repo, branch) での突き合わせは成立する。"""
    db_path = _init_db(tmp_path, monkeypatch, with_patterns=False)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, repo, branch)"
            " VALUES ('s-1', 'example/repo', 'feat/SAMPLE-1')"
        )
        conn.execute(
            "INSERT INTO pull_requests (repo, pr_number, head_branch)"
            " VALUES ('example/repo', 1, 'feat/SAMPLE-1')"
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["link"])

    assert result.exit_code == 0
    assert config.CONFIG_KEY_ISSUE_KEY_PATTERNS in result.output
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM session_pr_links").fetchone() == (1,)
        assert conn.execute("SELECT issue_key FROM sessions").fetchone() == (None,)
    finally:
        conn.close()


def test_log_reference_wins_over_branch_match_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """収集 → 突き合わせ → 参照が通しで動き、確実な由来が優先されることを確認する。"""
    db_path = _init_db(tmp_path, monkeypatch)
    monkeypatch.setattr(session, "DEFAULT_PROJECTS_DIR", FIXTURES_DIR)
    assert runner.invoke(app, ["backfill", "sessions"]).exit_code == 0

    conn = sqlite3.connect(db_path)
    try:
        # ログが PR 42 を参照しているセッションのブランチに一致する別の PR を置く。
        conn.execute(
            "INSERT INTO pull_requests (repo, pr_number, head_branch)"
            " VALUES ('acme/widget', 99, 'feat/pr-ref')"
        )
        conn.commit()
    finally:
        conn.close()

    assert runner.invoke(app, ["link"]).exit_code == 0
    result = runner.invoke(
        app,
        [
            "query",
            "SELECT pr_number, link_source FROM session_pr_links"
            " WHERE session_id = '99999999-9999-9999-9999-999999999999'",
        ],
    )

    assert result.exit_code == 0
    assert "42\tlog_reference" in result.output
    assert "99\trepo_branch" not in result.output


def test_query_prints_header_and_rows(tmp_path: Path, monkeypatch) -> None:
    db_path = _init_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, repo, branch, turns)"
            " VALUES ('s-1', 'example/repo', 'feat/x', 4)"
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["query", "SELECT session_id, turns, tool_calls FROM sessions"])

    assert result.exit_code == 0
    assert "session_id\tturns\ttool_calls" in result.output
    # NULL を空文字にしない（0 と観測できなかったことを区別する）
    assert "s-1\t4\tNULL" in result.output


def test_query_rejects_writes(tmp_path: Path, monkeypatch) -> None:
    db_path = _init_db(tmp_path, monkeypatch)

    result = runner.invoke(app, ["query", "DELETE FROM sessions"])

    assert result.exit_code == 1
    assert "SQL エラー" in result.output
    conn = sqlite3.connect(db_path)
    try:
        # 参照の逃げ道で raw 層を壊せない（A-002）
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
    finally:
        conn.close()


def test_query_reports_sql_errors_without_traceback(tmp_path: Path, monkeypatch) -> None:
    _init_db(tmp_path, monkeypatch)

    result = runner.invoke(app, ["query", "SELECT * FROM no_such_table"])

    assert result.exit_code == 1
    assert "SQL エラー" in result.output
