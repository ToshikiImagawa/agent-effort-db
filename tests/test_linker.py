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

REPO = "example/repo"


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "effort.db")
    schema.init_db(conn)
    return conn


def _insert_session(
    conn: sqlite3.Connection,
    session_id: str,
    branch: str | None = None,
    issue_key: str | None = None,
    repo: str | None = REPO,
) -> None:
    conn.execute(
        "INSERT INTO sessions (session_id, repo, branch, issue_key) VALUES (?, ?, ?, ?)",
        (session_id, repo, branch, issue_key),
    )
    conn.commit()


def _insert_pr(
    conn: sqlite3.Connection,
    pr_number: int,
    issue_key: str | None = None,
    head_branch: str | None = None,
    repo: str = REPO,
) -> None:
    conn.execute(
        "INSERT INTO pull_requests (repo, pr_number, head_branch, issue_key) VALUES (?, ?, ?, ?)",
        (repo, pr_number, head_branch, issue_key),
    )
    conn.commit()


def _insert_pr_ref(
    conn: sqlite3.Connection, session_id: str, pr_number: int, repo: str = REPO
) -> None:
    conn.execute(
        "INSERT INTO session_pr_refs (session_id, repo, pr_number) VALUES (?, ?, ?)",
        (session_id, repo, pr_number),
    )
    conn.commit()


def _links(conn: sqlite3.Connection) -> dict[tuple[str, int], str]:
    rows = conn.execute("SELECT session_id, pr_number, link_source FROM session_pr_links")
    return {(session_id, pr_number): source for session_id, pr_number, source in rows}


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


def test_links_by_repo_and_branch(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/x")
        _insert_pr(conn, 1, head_branch="feat/x")

        result = linker.resolve_links(conn, patterns=[])

        assert _links(conn) == {("s-1", 1): "repo_branch"}
        assert result.linked_by_source[linker.LinkSource.REPO_BRANCH] == 1
        assert result.unlinked_sessions == 0
    finally:
        conn.close()


def test_does_not_link_across_repositories_with_the_same_branch_name(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/x", repo="example/other")
        _insert_pr(conn, 1, head_branch="feat/x")

        linker.resolve_links(conn, patterns=[])

        assert _links(conn) == {}
    finally:
        conn.close()


def test_log_reference_is_applied_even_when_the_pr_is_not_collected(tmp_path: Path) -> None:
    """ログ内の PR 参照は PR を backfill したかに左右されない観測事実として扱う。"""
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/x")
        _insert_pr_ref(conn, "s-1", 7)

        linker.resolve_links(conn, patterns=[])

        assert _links(conn) == {("s-1", 7): "log_reference"}
    finally:
        conn.close()


def test_later_stage_does_not_overwrite_an_earlier_stage(tmp_path: Path) -> None:
    """由来の確実性の順序を保つ。ログ参照が付いたセッションに repo_branch は付けない。"""
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/x")
        _insert_pr_ref(conn, "s-1", 7)
        _insert_pr(conn, 7, head_branch="feat/x")
        _insert_pr(conn, 8, head_branch="feat/x")

        result = linker.resolve_links(conn, patterns=[])

        assert _links(conn) == {("s-1", 7): "log_reference"}
        assert result.linked_by_source[linker.LinkSource.REPO_BRANCH] == 0
    finally:
        conn.close()


def test_one_session_can_link_to_several_prs_by_branch(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/x")
        _insert_pr(conn, 1, head_branch="feat/x")
        _insert_pr(conn, 2, head_branch="feat/x")

        linker.resolve_links(conn, patterns=[])

        assert _links(conn) == {("s-1", 1): "repo_branch", ("s-1", 2): "repo_branch"}
    finally:
        conn.close()


def test_sessions_without_a_key_stay_unlinked(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch=None)
        _insert_session(conn, "s-2", branch="feat/x", repo=None)
        _insert_pr(conn, 1, head_branch="feat/x")

        result = linker.resolve_links(conn, patterns=[])

        assert _links(conn) == {}
        assert result.unlinked_sessions == 2
        # 行は消えない（A-004: 未紐付けも観測対象）
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (2,)
    finally:
        conn.close()


def test_resolve_links_is_idempotent(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/x")
        _insert_pr_ref(conn, "s-1", 7)
        _insert_session(conn, "s-2", branch="feat/y")
        _insert_pr(conn, 9, head_branch="feat/y")

        first = linker.resolve_links(conn, patterns=[])
        second = linker.resolve_links(conn, patterns=[])

        assert sum(first.linked_by_source.values()) == 2
        # 2 回目に増える分は無い。件数はそのまま「今回増えた分」を表す。
        assert sum(second.linked_by_source.values()) == 0
        assert len(_links(conn)) == 2
    finally:
        conn.close()


def test_branch_is_not_a_join_key_without_a_matching_pr(tmp_path: Path) -> None:
    """チケットキーが一致しても、それだけではリンクを作らない（issue_key は補助キー）。"""
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/SAMPLE-1", issue_key="SAMPLE-1")
        _insert_pr(conn, 1, issue_key="SAMPLE-1", head_branch="other/branch")

        linker.resolve_links(conn, patterns=PATTERNS)

        assert _links(conn) == {}
    finally:
        conn.close()


def test_issue_keys_are_assigned_independently_of_links(tmp_path: Path) -> None:
    """リンクの不成立とチケットキーの不在は独立した事象（A-004）。"""
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/SAMPLE-1-add")
        _insert_pr(conn, 1, head_branch="feat/SAMPLE-2-fix")

        result = linker.resolve_links(conn, patterns=PATTERNS)

        assert _links(conn) == {}
        assert result.issue_keys_assigned == 2
        assert conn.execute("SELECT issue_key FROM sessions").fetchone() == ("SAMPLE-1",)
        assert conn.execute("SELECT issue_key FROM pull_requests").fetchone() == ("SAMPLE-2",)
    finally:
        conn.close()


def test_issue_keys_are_not_assigned_without_patterns(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/SAMPLE-1-add")

        result = linker.resolve_links(conn, patterns=[])

        assert result.issue_keys_assigned == 0
        assert conn.execute("SELECT issue_key FROM sessions").fetchone() == (None,)
    finally:
        conn.close()


def test_assigning_issue_keys_is_idempotent(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/SAMPLE-1-add")

        first = linker.assign_issue_keys(conn, patterns=PATTERNS)
        second = linker.assign_issue_keys(conn, patterns=PATTERNS)

        assert (first, second) == (1, 0)
        assert conn.execute("SELECT issue_key FROM sessions").fetchone() == ("SAMPLE-1",)
    finally:
        conn.close()


def test_existing_issue_key_is_never_overwritten(tmp_path: Path) -> None:
    """付与済みの情報を失わない（A-004）。別の情報源で埋めた値を壊さない。"""
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/SAMPLE-1-add", issue_key="SAMPLE-9")

        assigned = linker.assign_issue_keys(conn, patterns=PATTERNS)

        assert assigned == 0
        assert conn.execute("SELECT issue_key FROM sessions").fetchone() == ("SAMPLE-9",)
    finally:
        conn.close()
