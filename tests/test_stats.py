from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from effort_db import linker, schema, stats

REPO = "example/repo"


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "effort.db")
    schema.init_db(conn)
    return conn


def _insert_session(
    conn: sqlite3.Connection,
    session_id: str,
    branch: str | None = None,
    repo: str | None = REPO,
) -> None:
    conn.execute(
        "INSERT INTO sessions (session_id, repo, branch) VALUES (?, ?, ?)",
        (session_id, repo, branch),
    )
    conn.commit()


def _insert_pr(
    conn: sqlite3.Connection, pr_number: int, head_branch: str | None, repo: str = REPO
) -> None:
    conn.execute(
        "INSERT INTO pull_requests (repo, pr_number, head_branch) VALUES (?, ?, ?)",
        (repo, pr_number, head_branch),
    )
    conn.commit()


def test_empty_db_reports_zero_without_dividing_by_zero(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        collected = stats.collect_stats(conn)

        assert collected.sessions == 0
        assert collected.join_rate_by_source[linker.LinkSource.UNLINKED] == 0.0
        assert collected.focused_join_rate == 0.0
    finally:
        conn.close()


def test_join_rate_is_reported_per_source(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/x")
        _insert_session(conn, "s-2", branch="feat/y")
        _insert_session(conn, "s-3", branch="feat/unlinked")
        _insert_session(conn, "s-4", branch=None)
        _insert_pr(conn, 1, head_branch="feat/y")
        conn.execute(
            "INSERT INTO session_pr_refs (session_id, repo, pr_number) VALUES ('s-1', ?, 7)",
            (REPO,),
        )
        conn.commit()
        linker.resolve_links(conn, patterns=[])

        collected = stats.collect_stats(conn)
        rates = collected.join_rate_by_source

        assert collected.linked_sessions_by_source[linker.LinkSource.LOG_REFERENCE] == 1
        assert collected.linked_sessions_by_source[linker.LinkSource.REPO_BRANCH] == 1
        assert collected.unlinked_sessions == 2
        assert rates[linker.LinkSource.LOG_REFERENCE] == pytest.approx(25.0)
        assert rates[linker.LinkSource.REPO_BRANCH] == pytest.approx(25.0)
        # 段が上書きしないため、由来ごとの率と未紐付けの率の合計は 100% になる
        assert sum(rates.values()) == pytest.approx(100.0)
    finally:
        conn.close()


def test_link_rows_and_linked_sessions_are_counted_separately(tmp_path: Path) -> None:
    """1 セッションが複数 PR に紐づくと行数とセッション数が乖離する。率はセッション数で出す。"""
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/x")
        _insert_pr(conn, 1, head_branch="feat/x")
        _insert_pr(conn, 2, head_branch="feat/x")
        linker.resolve_links(conn, patterns=[])

        collected = stats.collect_stats(conn)

        assert collected.links_by_source[linker.LinkSource.REPO_BRANCH] == 2
        assert collected.linked_sessions_by_source[linker.LinkSource.REPO_BRANCH] == 1
        assert collected.join_rate_by_source[linker.LinkSource.REPO_BRANCH] == pytest.approx(100.0)
    finally:
        conn.close()


def test_focused_rate_excludes_sessions_of_repositories_without_prs(tmp_path: Path) -> None:
    """PR 未収集のリポジトリが分母を薄めて join 率が低く見えるのを切り分ける。"""
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/x")
        _insert_session(conn, "s-2", branch="feat/x", repo="example/no-prs")
        _insert_session(conn, "s-3", branch="feat/x", repo="example/no-prs")
        _insert_pr(conn, 1, head_branch="feat/x")
        linker.resolve_links(conn, patterns=[])

        collected = stats.collect_stats(conn)

        assert collected.join_rate_by_source[linker.LinkSource.REPO_BRANCH] == pytest.approx(
            33.333, abs=0.01
        )
        assert collected.sessions_in_repos_with_prs == 1
        assert collected.focused_join_rate == pytest.approx(100.0)
    finally:
        conn.close()


def test_sessions_missing_a_join_key_are_counted(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch=None)
        _insert_session(conn, "s-2", branch="feat/x", repo=None)
        _insert_session(conn, "s-3", branch="feat/x")

        collected = stats.collect_stats(conn)

        assert collected.sessions_without_repo_or_branch == 2
    finally:
        conn.close()


def test_unknown_link_source_is_counted_separately_instead_of_failing(tmp_path: Path) -> None:
    """将来のバージョンが書いた由来で落とさない（内訳が全体と合わないことは示す）。"""
    conn = _open_db(tmp_path)
    try:
        _insert_session(conn, "s-1", branch="feat/x")
        conn.execute(
            "INSERT INTO session_pr_links (session_id, repo, pr_number, link_source)"
            " VALUES ('s-1', ?, 1, 'future_source')",
            (REPO,),
        )
        conn.commit()

        collected = stats.collect_stats(conn)

        assert collected.links_with_unknown_source == 1
        assert collected.links_by_source[linker.LinkSource.REPO_BRANCH] == 0
        # リンク自体は存在するので unlinked には数えない
        assert collected.unlinked_sessions == 0
    finally:
        conn.close()


def test_head_branch_coverage_is_reported(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        _insert_pr(conn, 1, head_branch="feat/x")
        _insert_pr(conn, 2, head_branch=None)

        collected = stats.collect_stats(conn)

        assert (collected.pull_requests, collected.prs_with_head_branch) == (2, 1)
    finally:
        conn.close()
