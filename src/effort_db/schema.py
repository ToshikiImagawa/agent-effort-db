"""DDL とスキーママイグレーション。

スキーマバージョンは PRAGMA user_version で管理する。専用テーブルではなく
これを選んだ理由: SQLite組み込みの整数値であり、追加テーブルやJOINなしで
同一トランザクション内で読み書きできるため、この用途には十分。

**件数列・トークン列に NOT NULL DEFAULT 0 を付けない。** メッセージが 1 件も無い
ログで「0 件」と断定できないため、0（観測できて 0 だった）と NULL（観測できなかった）
を区別する（design D18）。
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

# テーブルと索引。CREATE TABLE IF NOT EXISTS なので既存 DB では何も起きない。
# 既存 DB への列追加は _add_missing_columns が担う。
_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id                      TEXT PRIMARY KEY,
  repo                            TEXT,
  branch                          TEXT,
  issue_key                       TEXT,
  started_at                      TEXT,
  ended_at                        TEXT,
  wall_clock_min                  REAL,
  turns                           INTEGER,
  tool_calls                      INTEGER,
  sidechain_tool_calls            INTEGER,
  input_tokens                    INTEGER,
  output_tokens                   INTEGER,
  cache_read_tokens               INTEGER,
  cache_creation_tokens           INTEGER,
  sidechain_input_tokens          INTEGER,
  sidechain_output_tokens         INTEGER,
  sidechain_cache_read_tokens     INTEGER,
  sidechain_cache_creation_tokens INTEGER,
  interrupted                     INTEGER DEFAULT 0,
  log_versions                    TEXT,
  skipped_records                 INTEGER,
  collected_at                    TEXT
);

CREATE TABLE IF NOT EXISTS pull_requests (
  repo          TEXT    NOT NULL,
  pr_number     INTEGER NOT NULL,
  head_branch   TEXT,
  issue_key     TEXT,
  additions     INTEGER,
  deletions     INTEGER,
  changed_files INTEGER,
  review_rounds INTEGER,
  created_at    TEXT,
  merged_at     TEXT,
  labels        TEXT,
  collected_at  TEXT,
  PRIMARY KEY (repo, pr_number)
);

-- セッションログに含まれていた PR 参照。ログから観測した事実そのものであり、
-- そこから導いたリンク（session_pr_links）とは別の層に置く。
CREATE TABLE IF NOT EXISTS session_pr_refs (
  session_id TEXT    NOT NULL,
  repo       TEXT    NOT NULL,
  pr_number  INTEGER NOT NULL,
  PRIMARY KEY (session_id, repo, pr_number)
);

-- セッションと PR の対応。どのキーで紐付いたかを link_source として必ず持つ。
CREATE TABLE IF NOT EXISTS session_pr_links (
  session_id  TEXT    NOT NULL,
  repo        TEXT    NOT NULL,
  pr_number   INTEGER NOT NULL,
  link_source TEXT    NOT NULL,
  linked_at   TEXT,
  PRIMARY KEY (session_id, repo, pr_number)
);
"""

# 索引はテーブルより後に作る。v1 から移行する場合、head_branch のような後から
# 追加される列を参照する索引は、列が揃う前に実行すると失敗する。
_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_sessions_repo_branch ON sessions (repo, branch);
CREATE INDEX IF NOT EXISTS idx_sessions_issue_key   ON sessions (issue_key);
CREATE INDEX IF NOT EXISTS idx_prs_repo_head_branch ON pull_requests (repo, head_branch);
CREATE INDEX IF NOT EXISTS idx_prs_issue_key        ON pull_requests (issue_key);
CREATE INDEX IF NOT EXISTS idx_links_source         ON session_pr_links (link_source);
CREATE INDEX IF NOT EXISTS idx_links_pr             ON session_pr_links (repo, pr_number);
"""

# ビューは毎回作り直す。列が増えたときに古い定義が残らないようにするため。
# raw 層のテーブルは決して落とさない。
_VIEW_DDL = """
DROP VIEW IF EXISTS task_effort;
DROP VIEW IF EXISTS effort_by_branch;
DROP VIEW IF EXISTS effort_by_issue;

CREATE VIEW effort_by_branch AS
SELECT s.repo,
       s.branch,
       COUNT(*)                     AS sessions,
       SUM(s.turns)                 AS total_turns,
       SUM(s.tool_calls)            AS total_tool_calls,
       SUM(s.sidechain_tool_calls)  AS total_sidechain_tool_calls,
       SUM(s.wall_clock_min)        AS total_min,
       SUM(s.input_tokens)          AS total_input_tokens,
       SUM(s.output_tokens)         AS total_output_tokens,
       SUM(s.cache_read_tokens)     AS total_cache_read_tokens,
       SUM(s.cache_creation_tokens) AS total_cache_creation_tokens,
       SUM(s.sidechain_input_tokens)          AS total_sidechain_input_tokens,
       SUM(s.sidechain_output_tokens)         AS total_sidechain_output_tokens,
       SUM(s.sidechain_cache_read_tokens)     AS total_sidechain_cache_read_tokens,
       SUM(s.sidechain_cache_creation_tokens) AS total_sidechain_cache_creation_tokens,
       (SELECT COUNT(DISTINCT l.pr_number)
        FROM session_pr_links l
        JOIN sessions s2 ON s2.session_id = l.session_id
        WHERE s2.repo = s.repo AND s2.branch = s.branch) AS linked_prs,
       (SELECT SUM(p.additions + p.deletions)
        FROM pull_requests p
        WHERE p.repo = s.repo
          AND p.pr_number IN (SELECT l.pr_number
                              FROM session_pr_links l
                              JOIN sessions s3 ON s3.session_id = l.session_id
                              WHERE s3.repo = s.repo AND s3.branch = s.branch)) AS diff_size
FROM sessions s
WHERE s.repo IS NOT NULL AND s.branch IS NOT NULL
GROUP BY s.repo, s.branch;

CREATE VIEW effort_by_issue AS
SELECT s.issue_key,
       COUNT(*)                     AS sessions,
       SUM(s.turns)                 AS total_turns,
       SUM(s.tool_calls)            AS total_tool_calls,
       SUM(s.sidechain_tool_calls)  AS total_sidechain_tool_calls,
       SUM(s.wall_clock_min)        AS total_min,
       SUM(s.input_tokens)          AS total_input_tokens,
       SUM(s.output_tokens)         AS total_output_tokens,
       SUM(s.cache_read_tokens)     AS total_cache_read_tokens,
       SUM(s.cache_creation_tokens) AS total_cache_creation_tokens,
       SUM(s.sidechain_input_tokens)          AS total_sidechain_input_tokens,
       SUM(s.sidechain_output_tokens)         AS total_sidechain_output_tokens,
       SUM(s.sidechain_cache_read_tokens)     AS total_sidechain_cache_read_tokens,
       SUM(s.sidechain_cache_creation_tokens) AS total_sidechain_cache_creation_tokens,
       (SELECT COUNT(DISTINCT l.pr_number)
        FROM session_pr_links l
        JOIN sessions s2 ON s2.session_id = l.session_id
        WHERE s2.issue_key = s.issue_key) AS linked_prs,
       (SELECT SUM(p.additions + p.deletions)
        FROM pull_requests p
        WHERE p.issue_key = s.issue_key) AS diff_size
FROM sessions s
WHERE s.issue_key IS NOT NULL
GROUP BY s.issue_key;
"""

# v1 に無く v2 で追加された列。宣言に NOT NULL を付けないのは D18 に従うためで、
# 既存行には「観測できなかった」ことを表す NULL が入る。
_V2_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "sessions": (
        ("sidechain_tool_calls", "INTEGER"),
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("cache_read_tokens", "INTEGER"),
        ("cache_creation_tokens", "INTEGER"),
        ("sidechain_input_tokens", "INTEGER"),
        ("sidechain_output_tokens", "INTEGER"),
        ("sidechain_cache_read_tokens", "INTEGER"),
        ("sidechain_cache_creation_tokens", "INTEGER"),
        ("log_versions", "TEXT"),
        ("skipped_records", "INTEGER"),
        ("collected_at", "TEXT"),
    ),
    "pull_requests": (
        ("head_branch", "TEXT"),
        ("collected_at", "TEXT"),
    ),
}


def init_db(conn: sqlite3.Connection) -> None:
    """テーブル/ビューを冪等に作成し、必要ならマイグレーションを適用する。

    既存データは保持される。ビューだけは作り直す（_VIEW_DDL のコメント参照）。
    """
    conn.executescript(_TABLE_DDL)
    migrate(conn)
    conn.executescript(_INDEX_DDL)
    conn.executescript(_VIEW_DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """現在の user_version から SCHEMA_VERSION まで順次適用する。

    段は版数の昇順に並べるだけの構造にしてある。新しい版を足すときは
    `if current < N:` の行を末尾に追加し、既存の段には手を入れない。
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current >= SCHEMA_VERSION:
        return
    if current < 2:
        _migrate_v1_to_v2(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 の列構成に v2 の列を加える。既存行は破棄しない。

    新規作成した DB（_TABLE_DDL で既に v2 の列を持つ）に対しては何もしない。
    列の有無で判定するため、新規作成と移行のどちらの経路でも同じ列構成に収束する。

    v1 の task_effort ビューは issue_key を主軸にしており実測に合わないため落とすが、
    その処理は _VIEW_DDL 側にまとめてある（ビューは毎回作り直すため）。
    """
    for table, columns in _V2_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
