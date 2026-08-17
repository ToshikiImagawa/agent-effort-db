from __future__ import annotations

import sqlite3
from pathlib import Path

from effort_db import schema

# v1 のスキーマ（履歴上の形）。移行を検証するために、実装から独立した文字列として持つ。
# schema.py の定数を参照すると「移行前の形」が変わったときにテストが一緒に変わってしまう。
_V1_DDL = """
CREATE TABLE sessions (
  session_id     TEXT PRIMARY KEY,
  repo           TEXT,
  branch         TEXT,
  issue_key      TEXT,
  started_at     TEXT,
  ended_at       TEXT,
  wall_clock_min REAL,
  turns          INTEGER,
  tool_calls     INTEGER,
  interrupted    INTEGER DEFAULT 0
);

CREATE TABLE pull_requests (
  pr_number     INTEGER,
  repo          TEXT,
  issue_key     TEXT,
  additions     INTEGER,
  deletions     INTEGER,
  changed_files INTEGER,
  review_rounds INTEGER,
  created_at    TEXT,
  merged_at     TEXT,
  labels        TEXT,
  PRIMARY KEY (repo, pr_number)
);

CREATE VIEW task_effort AS
SELECT issue_key, COUNT(*) AS sessions FROM sessions GROUP BY issue_key;
"""


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def _view_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall()
    return {row[0] for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _view_columns(conn: sqlite3.Connection, view: str) -> list[str]:
    return [description[0] for description in conn.execute(f"SELECT * FROM {view}").description]


def _connect_v1(db_path: Path) -> sqlite3.Connection:
    """v1 の DB を作って返す。"""
    conn = sqlite3.connect(db_path)
    conn.executescript(_V1_DDL)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    return conn


def test_init_db_creates_raw_link_and_aggregate_layers(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "effort.db")
    try:
        schema.init_db(conn)

        assert {"sessions", "pull_requests", "session_pr_refs", "session_pr_links"} <= _table_names(
            conn
        )
        assert {"effort_by_branch", "effort_by_issue"} <= _view_names(conn)
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


def test_migration_from_v1_preserves_rows_and_adds_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "effort.db"
    conn = _connect_v1(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, issue_key, turns) VALUES (?, ?, ?)",
            ("session-1", "SAMPLE-1", 5),
        )
        conn.commit()

        schema.init_db(conn)

        # 既存行は失われず、v1 に無かった列は「観測できなかった」ことを表す NULL になる
        row = conn.execute(
            "SELECT issue_key, turns, sidechain_tool_calls, cache_creation_tokens"
            " FROM sessions WHERE session_id = ?",
            ("session-1",),
        ).fetchone()
        assert row == ("SAMPLE-1", 5, None, None)
        assert "head_branch" in _columns(conn, "pull_requests")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_replaces_task_effort_view(tmp_path: Path) -> None:
    conn = _connect_v1(tmp_path / "effort.db")
    try:
        assert "task_effort" in _view_names(conn)

        schema.init_db(conn)

        assert "task_effort" not in _view_names(conn)
        assert {"effort_by_branch", "effort_by_issue"} <= _view_names(conn)
    finally:
        conn.close()


def test_fresh_and_migrated_databases_have_the_same_columns(tmp_path: Path) -> None:
    """新規作成と v1 からの移行が同じ列構成に収束する（経路によって差が出ない）。"""
    fresh = sqlite3.connect(tmp_path / "fresh.db")
    migrated = _connect_v1(tmp_path / "migrated.db")
    try:
        schema.init_db(fresh)
        schema.init_db(migrated)

        for table in ("sessions", "pull_requests"):
            assert _columns(fresh, table) == _columns(migrated, table)
    finally:
        fresh.close()
        migrated.close()


def test_aggregate_views_share_columns_except_the_grouping_key(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "effort.db")
    try:
        schema.init_db(conn)

        by_branch = _view_columns(conn, "effort_by_branch")
        by_issue = _view_columns(conn, "effort_by_issue")

        assert by_branch[:2] == ["repo", "branch"]
        assert by_issue[:1] == ["issue_key"]
        assert by_branch[2:] == by_issue[1:]
    finally:
        conn.close()


def test_aggregate_view_keeps_main_and_subagent_separable_and_summable(tmp_path: Path) -> None:
    """別列で保持しても合算値はビューで得られる（design D2 / D19 の前提）。"""
    conn = sqlite3.connect(tmp_path / "effort.db")
    try:
        schema.init_db(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, repo, branch, tool_calls, sidechain_tool_calls,"
            " input_tokens, sidechain_input_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("session-1", "acme/widget", "feat/x", 2, 3, 100, 50),
        )
        conn.commit()

        row = conn.execute(
            "SELECT total_tool_calls, total_sidechain_tool_calls,"
            " total_tool_calls + total_sidechain_tool_calls AS combined_tool_calls,"
            " total_input_tokens + total_sidechain_input_tokens AS combined_input_tokens"
            " FROM effort_by_branch"
        ).fetchone()

        assert row == (2, 3, 5, 150)
    finally:
        conn.close()


_PERCENTILE_SQL = """
SELECT MIN(CASE WHEN pct >= 0.5 THEN turns END) AS median_turns,
       MIN(CASE WHEN pct >= 0.9 THEN turns END) AS p90_turns
FROM (SELECT turns, CUME_DIST() OVER (ORDER BY turns) AS pct FROM sessions WHERE turns > 0)
"""


def test_distribution_can_be_derived_from_session_rows(tmp_path: Path) -> None:
    """集約が合計値だけでも、raw 層が行を保持しているので分布が出せる（B-003）。

    分位点は「累積分布が p に達する最小の値」で求める。同じ値が過半を占める分布
    （実データではターン数 1 が 51.4%）では `MAX(... <= p)` が NULL になるため。
    """
    conn = sqlite3.connect(tmp_path / "effort.db")
    try:
        schema.init_db(conn)
        # 最小値（1）が過半を占める分布にする
        turns = [1, 1, 1, 1, 1, 1, 2, 5, 9, 30]
        conn.executemany(
            "INSERT INTO sessions (session_id, turns) VALUES (?, ?)",
            [(f"session-{index}", value) for index, value in enumerate(turns)],
        )
        conn.commit()

        # 中央値は 1（累積 0.6 で既に 0.5 を超える）、p90 は 9（累積がちょうど 0.9）
        assert conn.execute(_PERCENTILE_SQL).fetchone() == (1, 9)
        # 誤った形（MAX(... <= p)）では中央値が取れないことを固定しておく
        broken = conn.execute(
            _PERCENTILE_SQL.replace("MIN(CASE WHEN pct >= 0.5", "MAX(CASE WHEN pct <= 0.5")
        ).fetchone()
        assert broken[0] is None
    finally:
        conn.close()


def test_effort_by_branch_does_not_double_count_sessions_linked_to_several_prs(
    tmp_path: Path,
) -> None:
    """1 セッションが複数 PR に紐づいても実測値が重複計上されない（実データで 11% 起きる）。"""
    conn = sqlite3.connect(tmp_path / "effort.db")
    try:
        schema.init_db(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, repo, branch, turns, tool_calls)"
            " VALUES (?, ?, ?, ?, ?)",
            ("session-1", "acme/widget", "feature/x", 10, 20),
        )
        for pr_number, additions in ((1, 100), (2, 200)):
            conn.execute(
                "INSERT INTO pull_requests (repo, pr_number, head_branch, additions, deletions)"
                " VALUES (?, ?, ?, ?, ?)",
                ("acme/widget", pr_number, "feature/x", additions, 0),
            )
            conn.execute(
                "INSERT INTO session_pr_links (session_id, repo, pr_number, link_source)"
                " VALUES (?, ?, ?, ?)",
                ("session-1", "acme/widget", pr_number, "repo_branch"),
            )
        conn.commit()

        row = conn.execute(
            "SELECT sessions, total_turns, total_tool_calls, linked_prs, diff_size"
            " FROM effort_by_branch"
        ).fetchone()
        assert row == (1, 10, 20, 2, 300)
    finally:
        conn.close()
