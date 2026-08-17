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
PR_REF = "99999999-9999-9999-9999-999999999999"
TOKENS = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

FIXTURE_SESSION_COUNT = 10

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
    # 主エージェントの tool_use は 2 件。isSidechain な 1 件と subagents/ の 1 件は
    # サブエージェント側に振り分ける（合算すると主エージェント分を復元できない）。
    assert record.tool_calls == 2
    assert record.sidechain_tool_calls == 2
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
            PR_REF,
            TOKENS,
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


def test_broken_lines_are_skipped_and_counted() -> None:
    record = _parse(REPO_PROJECT_DIR, BROKEN)

    assert record.turns == 1
    assert record.tool_calls == 1
    assert record.wall_clock_min == 1.0
    # 壊れた行・レコードでない JSON は件数として残す（黙って捨てない）
    assert record.skipped_records == 3


def test_tokens_are_separated_between_main_and_subagents() -> None:
    """合算すると主エージェント分を後から復元できない（design D19）。"""
    record = _parse(REPO_PROJECT_DIR, TOKENS)

    main = (
        record.input_tokens,
        record.output_tokens,
        record.cache_read_tokens,
        record.cache_creation_tokens,
    )
    sidechain = (
        record.sidechain_input_tokens,
        record.sidechain_output_tokens,
        record.sidechain_cache_read_tokens,
        record.sidechain_cache_creation_tokens,
    )

    assert main == (100, 20, 1000, 50)
    # 本体の isSidechain 分（7,3,9,1）と subagents/ 分（5,2,6,4）の合計
    assert sidechain == (12, 5, 15, 5)


def test_non_numeric_usage_values_are_ignored_without_raising() -> None:
    """usage の値が数値でなくても収集を止めない（FR-020）。"""
    record = _parse(REPO_PROJECT_DIR, TOKENS)

    # 3 番目の assistant は usage が文字列 / null / bool。加算されていない
    assert record.input_tokens == 100
    assert record.cache_read_tokens == 1000


def test_tool_calls_and_tokens_agree_on_what_counts_as_a_subagent() -> None:
    record = _parse(REPO_PROJECT_DIR, TOKENS)

    assert record.tool_calls == 1
    # 本体の isSidechain 1 件 + subagents/ の 2 件
    assert record.sidechain_tool_calls == 3


def test_observed_log_versions_are_kept() -> None:
    record = _parse(REPO_PROJECT_DIR, TOKENS)

    # 混在するバージョンをすべて残す（どのバージョンで欠損が出たか追えるように）
    assert record.log_versions == "0.0.0-fixture,0.0.1-fixture"


def test_sessions_without_subagents_report_zero_not_null() -> None:
    """観測できて 0 だった場合と、観測できなかった場合を区別する（design D18）。"""
    with_messages = _parse(REPO_PROJECT_DIR, BROKEN)
    empty = _parse(REPO_PROJECT_DIR, EMPTY)

    assert with_messages.sidechain_tool_calls == 0
    assert with_messages.input_tokens == 0
    assert empty.sidechain_tool_calls is None
    assert empty.input_tokens is None


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


def test_pr_references_are_collected_from_the_log() -> None:
    record = _parse(REPO_PROJECT_DIR, PR_REF)

    # 同じ PR への参照が複数回現れても 1 件。別リポジトリの PR も参照として残す。
    assert record.pr_refs == (("acme/widget", 42), ("acme/other", 43))


def test_unparsable_pr_references_are_skipped_without_stopping_collection() -> None:
    """PR 番号が数値でない・リポジトリが owner/repo でない・キーが無い場合は落とす。"""
    record = _parse(REPO_PROJECT_DIR, PR_REF)

    assert all(isinstance(pr_number, int) for _, pr_number in record.pr_refs)
    # 壊れた参照があっても他の観測値は取れている
    assert record.branch == "feat/pr-ref"
    assert record.turns == 1


def test_pr_reference_records_are_not_counted_as_turns_or_tool_calls() -> None:
    record = _parse(REPO_PROJECT_DIR, PR_REF)

    assert (record.turns, record.tool_calls) == (1, 1)


def test_pr_references_are_stored_and_do_not_duplicate_on_recollection(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "effort.db")
    try:
        record = _parse(REPO_PROJECT_DIR, PR_REF)
        session.upsert_session(conn, record)
        session.upsert_session(conn, record)
        conn.commit()

        rows = conn.execute(
            "SELECT session_id, repo, pr_number FROM session_pr_refs ORDER BY pr_number"
        ).fetchall()

        assert rows == [(PR_REF, "acme/widget", 42), (PR_REF, "acme/other", 43)]
    finally:
        conn.close()


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


def test_upsert_records_when_the_data_was_collected(tmp_path: Path) -> None:
    """`collected_at` は「いつ時点のデータか」を示す観測値。再収集で更新される。"""
    conn = _connect(tmp_path / "effort.db")
    try:
        session.upsert_session(conn, _parse(REPO_PROJECT_DIR, TOKENS))
        conn.commit()

        row = conn.execute(
            "SELECT collected_at, log_versions, skipped_records, sidechain_tool_calls"
            " FROM sessions WHERE session_id = ?",
            (TOKENS,),
        ).fetchone()

        assert row[0] is not None
        assert row[1:] == ("0.0.0-fixture,0.0.1-fixture", 0, 3)
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
