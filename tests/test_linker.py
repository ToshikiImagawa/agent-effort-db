from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from effort_db import config, linker, schema

# 実装は既定パターンを持たない（CONSTITUTION B-004）ため、テスト側で合成する。
# キーは実在しない架空の値のみを使う。
TICKET_PATTERN = r"(?<![0-9A-Za-z])[A-Z][A-Z0-9]+-[0-9]+(?![0-9A-Za-z])"
ISSUE_NUMBER_PATTERN = r"(?<![0-9A-Za-z])#[0-9]+(?![0-9A-Za-z])"
PATTERNS = linker.compile_patterns([TICKET_PATTERN, ISSUE_NUMBER_PATTERN])


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "effort.db")
    schema.init_db(conn)
    return conn


def _insert_session(
    conn: sqlite3.Connection,
    session_id: str,
    branch: str | None = None,
    issue_key: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO sessions (session_id, branch, issue_key) VALUES (?, ?, ?)",
        (session_id, branch, issue_key),
    )
    conn.commit()


def _insert_pr(conn: sqlite3.Connection, pr_number: int, issue_key: str | None) -> None:
    conn.execute(
        "INSERT INTO pull_requests (repo, pr_number, issue_key) VALUES (?, ?, ?)",
        ("example/repo", pr_number, issue_key),
    )
    conn.commit()


def test_extracts_prefixed_ticket_key_from_branch() -> None:
    key = linker.extract_issue_key("feat/SAMPLE-123-add-thing", patterns=PATTERNS)

    assert key == "SAMPLE-123"


def test_extracts_github_issue_number_from_commit_message() -> None:
    key = linker.extract_issue_key("fix crash (#456)", patterns=PATTERNS)

    assert key == "#456"


def test_prefixed_key_wins_over_issue_number_in_same_text() -> None:
    key = linker.extract_issue_key("fix #456 for SAMPLE-1", patterns=PATTERNS)

    assert key == "SAMPLE-1"


def test_only_leftmost_key_is_used() -> None:
    key = linker.extract_issue_key("SAMPLE-1 and SAMPLE-2", patterns=PATTERNS)

    assert key == "SAMPLE-1"


def test_branch_candidate_takes_priority_over_commit_message() -> None:
    key = linker.extract_issue_key(
        "feat/SAMPLE-1-add-thing", "revert of SAMPLE-999", patterns=PATTERNS
    )

    assert key == "SAMPLE-1"


def test_returns_none_when_no_pattern_matches() -> None:
    assert linker.extract_issue_key("feat/add-linker", patterns=PATTERNS) is None


def test_none_and_empty_candidates_do_not_raise() -> None:
    assert linker.extract_issue_key(None, patterns=PATTERNS) is None
    assert linker.extract_issue_key(None, "", patterns=PATTERNS) is None
    assert linker.extract_issue_key(None, "SAMPLE-2", patterns=PATTERNS) == "SAMPLE-2"


def test_matched_text_is_returned_without_normalization() -> None:
    patterns = linker.compile_patterns([r"ticket_[0-9]+"])

    assert linker.extract_issue_key("feat/ticket_42", patterns=patterns) == "ticket_42"


def test_patterns_come_from_config_toml(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / config.CONFIG_FILE_NAME).write_text(
        'issue_key_patterns = ["TASK/[0-9]+"]\n', encoding="utf-8"
    )

    patterns = linker.resolve_patterns(data_dir=data_dir)

    assert linker.extract_issue_key("feat/TASK/7", patterns=patterns) == "TASK/7"
    # 設定したパターン以外は抽出されない。
    assert linker.extract_issue_key("feat/SAMPLE-1", patterns=patterns) is None


def test_no_patterns_when_config_absent(tmp_path: Path) -> None:
    """既定パターンを持たない（CONSTITUTION B-004）。未設定なら抽出しない。"""
    patterns = linker.resolve_patterns(data_dir=tmp_path / "missing")

    assert patterns == []
    assert linker.extract_issue_key("feat/SAMPLE-1", patterns=patterns) is None


def test_relink_with_no_patterns_keeps_all_rows_unlinked(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/SAMPLE-1-add")

        result = linker.relink_sessions(conn, patterns=[])

        assert result == linker.RelinkResult(scanned=1, updated=0, unlinked=1)
        assert conn.execute("SELECT issue_key FROM sessions").fetchone() == (None,)
    finally:
        conn.close()


def test_invalid_pattern_raises_value_error() -> None:
    with pytest.raises(ValueError):
        linker.compile_patterns(["SAMPLE-(["])


def test_non_list_patterns_in_config_raises_value_error(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / config.CONFIG_FILE_NAME).write_text(
        'issue_key_patterns = "SAMPLE-[0-9]+"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError):
        linker.resolve_patterns(data_dir=data_dir)


def test_relink_fills_issue_key_and_keeps_unlinked_rows(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/SAMPLE-1-add")
        _insert_session(conn, "s-2", branch="feat/no-key")
        _insert_session(conn, "s-3", branch=None)

        result = linker.relink_sessions(conn, patterns=PATTERNS)

        assert result == linker.RelinkResult(scanned=3, updated=1, unlinked=2)
        rows = dict(conn.execute("SELECT session_id, issue_key FROM sessions").fetchall())
        assert rows == {"s-1": "SAMPLE-1", "s-2": None, "s-3": None}
    finally:
        conn.close()


def test_relink_is_idempotent(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/SAMPLE-1-add")

        first = linker.relink_sessions(conn, patterns=PATTERNS)
        second = linker.relink_sessions(conn, patterns=PATTERNS)

        assert first.updated == 1
        assert second.updated == 0
        assert second.scanned == 1
        assert conn.execute("SELECT issue_key FROM sessions").fetchone() == ("SAMPLE-1",)
    finally:
        conn.close()


def test_relink_does_not_clear_existing_key_when_branch_has_none(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/no-key", issue_key="SAMPLE-9")

        result = linker.relink_sessions(conn, patterns=PATTERNS)

        assert result.updated == 0
        assert result.unlinked == 0
        assert conn.execute("SELECT issue_key FROM sessions").fetchone() == ("SAMPLE-9",)
    finally:
        conn.close()


def test_collect_link_stats_on_empty_db(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        stats = linker.collect_link_stats(conn)

        assert stats == linker.LinkStats(
            sessions_total=0,
            sessions_linked=0,
            prs_total=0,
            prs_linked=0,
            task_effort_keys=0,
            joined_keys=0,
            sessions_only_keys=0,
            prs_only_keys=0,
        )
        assert stats.sessions_link_rate == 0.0
        assert stats.prs_link_rate == 0.0
    finally:
        conn.close()


def test_collect_link_stats_counts_join_state(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/SAMPLE-1", issue_key="SAMPLE-1")
        _insert_session(conn, "s-2", branch="feat/SAMPLE-2", issue_key="SAMPLE-2")
        _insert_session(conn, "s-3", branch="feat/no-key")
        _insert_pr(conn, 1, "SAMPLE-1")
        _insert_pr(conn, 2, "SAMPLE-3")
        _insert_pr(conn, 3, None)

        stats = linker.collect_link_stats(conn)

        assert stats.sessions_total == 3
        assert stats.sessions_linked == 2
        assert stats.sessions_link_rate == pytest.approx(66.666, abs=0.01)
        assert stats.prs_total == 3
        assert stats.prs_linked == 2
        assert stats.task_effort_keys == 2
        assert stats.joined_keys == 1
        assert stats.sessions_only_keys == 1
        assert stats.prs_only_keys == 1
    finally:
        conn.close()
