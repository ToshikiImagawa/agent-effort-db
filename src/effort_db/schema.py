"""DDL とスキーママイグレーション。

スキーマバージョンは PRAGMA user_version で管理する。専用テーブルではなく
これを選んだ理由: SQLite組み込みの整数値であり、追加テーブルやJOINなしで
同一トランザクション内で読み書きできるため、この用途には十分。
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
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

CREATE INDEX IF NOT EXISTS idx_sessions_issue_key ON sessions (issue_key);

CREATE TABLE IF NOT EXISTS pull_requests (
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

CREATE INDEX IF NOT EXISTS idx_pull_requests_issue_key ON pull_requests (issue_key);

CREATE VIEW IF NOT EXISTS task_effort AS
SELECT issue_key,
       COUNT(DISTINCT s.session_id) AS sessions,
       SUM(s.turns)          AS total_turns,
       SUM(s.wall_clock_min) AS total_min,
       SUM(p.additions + p.deletions) AS diff_size
FROM sessions s
LEFT JOIN pull_requests p USING (issue_key)
GROUP BY issue_key;
"""


def init_db(conn: sqlite3.Connection) -> None:
    """テーブル/ビューを冪等に作成する。既存データは保持される。"""
    conn.executescript(_DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
