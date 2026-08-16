from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from typer.testing import CliRunner

from effort_db import config, schema
from effort_db.cli import app
from effort_db.collectors import session

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "projects"
REPO_PROJECT_DIR = FIXTURES_DIR / "-Users-dev-src-example-com-acme-widget"
OUTSIDE_PROJECT_DIR = FIXTURES_DIR / "-private-tmp-scratch"

NORMAL = "11111111-1111-1111-1111-111111111111"
SPARSE = "22222222-2222-2222-2222-222222222222"
EMPTY = "33333333-3333-3333-3333-333333333333"
BROKEN = "44444444-4444-4444-4444-444444444444"
INTERRUPT_FIELD = "55555555-5555-5555-5555-555555555555"
INTERRUPT_TEXT = "66666666-6666-6666-6666-666666666666"
OUTSIDE_REPO = "77777777-7777-7777-7777-777777777777"
BRANCH_SWITCH = "88888888-8888-8888-8888-888888888888"

FIXTURE_SESSION_COUNT = 8

runner = CliRunner()


def _parse(project_dir: Path, session_id: str) -> session.SessionRecord:
    return session.parse_session_file(project_dir / f"{session_id}.jsonl")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    schema.init_db(conn)
    return conn


def test_parses_normal_session() -> None:
    record = _parse(REPO_PROJECT_DIR, NORMAL)

    assert record.session_id == NORMAL
    assert record.repo == "acme/widget"
    assert record.branch == "feat/sample"
    assert record.started_at == "2026-01-05T10:00:00+00:00"
    assert record.ended_at == "2026-01-05T10:10:00+00:00"
    assert record.wall_clock_min == 10.0
    # 人間のプロンプト 2 件のみ。tool_result / isMeta / isSidechain の user は除外。
    assert record.turns == 2
    # 主トランスクリプトの tool_use 3 件 + subagents/ の 1 件。
    assert record.tool_calls == 4
    assert record.interrupted is False


def test_subagent_transcripts_do_not_create_rows() -> None:
    # subagents/*.jsonl と workflows/**/journal.jsonl はセッションではないので
    # 行を作らない（特に journal.jsonl は複数セッションで同名になる）。
    listed = [path.stem for path in session.iter_session_files(FIXTURES_DIR)]

    assert sorted(listed) == sorted(
        [
            NORMAL,
            SPARSE,
            EMPTY,
            BROKEN,
            INTERRUPT_FIELD,
            INTERRUPT_TEXT,
            OUTSIDE_REPO,
            BRANCH_SWITCH,
        ]
    )


def test_missing_fields_become_null_and_row_is_still_built() -> None:
    record = _parse(REPO_PROJECT_DIR, SPARSE)

    assert record.started_at is None
    assert record.ended_at is None
    assert record.wall_clock_min is None
    assert record.branch is None
    assert record.turns == 1
    assert record.tool_calls == 1


def test_empty_file_yields_row_with_null_metrics() -> None:
    record = _parse(REPO_PROJECT_DIR, EMPTY)

    assert record.session_id == EMPTY
    assert record.turns is None
    assert record.tool_calls is None
    assert record.started_at is None
    assert record.interrupted is False


def test_broken_lines_are_skipped() -> None:
    record = _parse(REPO_PROJECT_DIR, BROKEN)

    assert record.turns == 1
    assert record.tool_calls == 1
    assert record.wall_clock_min == 1.0


def test_interrupted_detected_from_structural_field() -> None:
    assert _parse(REPO_PROJECT_DIR, INTERRUPT_FIELD).interrupted is True


def test_interrupted_detected_from_text_marker() -> None:
    assert _parse(REPO_PROJECT_DIR, INTERRUPT_TEXT).interrupted is True


def test_repo_falls_back_to_project_dir_name_without_cwd() -> None:
    # cwd を持つエントリが 1 件も無いログ。owner/repo に復元できないため
    # ディレクトリ名をそのまま入れて行を残す。
    assert _parse(REPO_PROJECT_DIR, SPARSE).repo == REPO_PROJECT_DIR.name


def test_repo_is_null_outside_host_layout() -> None:
    assert _parse(OUTSIDE_PROJECT_DIR, OUTSIDE_REPO).repo is None


def test_branch_uses_most_frequent_value() -> None:
    assert _parse(REPO_PROJECT_DIR, BRANCH_SWITCH).branch == "main"


def test_collect_all_is_idempotent(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "effort.db")
    try:
        first = session.collect_all(conn, FIXTURES_DIR)
        count_after_first = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        second = session.collect_all(conn, FIXTURES_DIR)
        count_after_second = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        assert first == second == FIXTURE_SESSION_COUNT
        assert count_after_first == count_after_second == FIXTURE_SESSION_COUNT
    finally:
        conn.close()


def test_upsert_updates_growing_session(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "effort.db")
    try:
        record = _parse(REPO_PROJECT_DIR, NORMAL)
        session.upsert_session(conn, record)
        session.upsert_session(
            conn,
            replace(record, turns=5, ended_at="2026-01-05T10:30:00+00:00", wall_clock_min=30.0),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT turns, ended_at, wall_clock_min FROM sessions WHERE session_id = ?",
            (NORMAL,),
        ).fetchall()

        assert rows == [(5, "2026-01-05T10:30:00+00:00", 30.0)]
    finally:
        conn.close()


def test_upsert_preserves_issue_key(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "effort.db")
    try:
        record = _parse(REPO_PROJECT_DIR, NORMAL)
        session.upsert_session(conn, record)
        conn.execute("UPDATE sessions SET issue_key = ? WHERE session_id = ?", ("SAMPLE-1", NORMAL))
        conn.commit()

        session.upsert_session(conn, record)
        conn.commit()

        row = conn.execute(
            "SELECT issue_key FROM sessions WHERE session_id = ?", (NORMAL,)
        ).fetchone()

        assert row == ("SAMPLE-1",)
    finally:
        conn.close()


def test_collect_one_targets_single_session(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "effort.db")
    try:
        assert session.collect_one(conn, NORMAL, FIXTURES_DIR) is True
        assert session.collect_one(conn, "no-such-session", FIXTURES_DIR) is False

        rows = conn.execute("SELECT session_id FROM sessions").fetchall()
        assert rows == [(NORMAL,)]
    finally:
        conn.close()


def test_iter_session_files_tolerates_missing_dir(tmp_path: Path) -> None:
    assert list(session.iter_session_files(tmp_path / "absent")) == []


def test_cli_backfill_sessions(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "effort.db"
    monkeypatch.setenv(config.ENV_DB_PATH, str(db_path))
    monkeypatch.setattr(session, "DEFAULT_PROJECTS_DIR", FIXTURES_DIR)

    result = runner.invoke(app, ["backfill", "sessions"])

    assert result.exit_code == 0
    assert f"collected {FIXTURE_SESSION_COUNT} sessions" in result.output
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == FIXTURE_SESSION_COUNT
    finally:
        conn.close()


def test_cli_collect_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_DB_PATH, str(tmp_path / "effort.db"))
    monkeypatch.setattr(session, "DEFAULT_PROJECTS_DIR", FIXTURES_DIR)

    result = runner.invoke(app, ["collect-session", NORMAL])

    assert result.exit_code == 0
    assert NORMAL in result.output


def test_cli_collect_session_reports_missing_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_DB_PATH, str(tmp_path / "effort.db"))
    monkeypatch.setattr(session, "DEFAULT_PROJECTS_DIR", FIXTURES_DIR)

    result = runner.invoke(app, ["collect-session", "no-such-session"])

    assert result.exit_code == 1
