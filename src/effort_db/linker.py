"""セッションと PR の突き合わせ（段階適用）と、チケットキーの抽出。

突き合わせキーは 1 種類ではない。**確実性の高い順に段階適用**し、どのキーで
紐付いたかを `link_source` として残す。実ログの観測から、チケットキーを含む
ブランチ名は 1 件も存在しないため、`issue_key` を主軸にすると 1 件も紐付かない
（design O10 / D6）。`issue_key` はチケット単位の集約に使う補助キーとして扱う。

紐付かなかったセッションは捨てず、リンクが無い状態（`unlinked`）で保持する。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from effort_db import config


class LinkSource(Enum):
    """突き合わせの由来。確実性の高い順に定義する。

    UNLINKED は「リンクが存在しない」ことを表す論理値であり、リンクの実体として
    永続化されることはない。未紐付け件数を由来別の内訳と同じ軸で扱うために定義する。
    """

    LOG_REFERENCE = "log_reference"
    REPO_BRANCH = "repo_branch"
    UNLINKED = "unlinked"


def compile_patterns(patterns: Sequence[str]) -> list[re.Pattern[str]]:
    """正規表現文字列をコンパイルする。不正なパターンは ValueError にして原因を示す。"""
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ValueError(f"issue_key パターンが不正な正規表現です: {pattern} ({exc})") from exc
    return compiled


def resolve_patterns(*, data_dir: Path | None = None) -> list[re.Pattern[str]]:
    """config.toml の issue_key_patterns をコンパイルして返す。未設定なら空リスト。

    既定パターンは持たない（CONSTITUTION B-004 / design)。チケットキーの形式を
    コードが仮定すると、組織固有・リポジトリ固有の慣習がソースに混入する。
    未設定時は抽出せず、issue_key は NULL のまま保持する。
    """
    configured = config.load_issue_key_patterns(data_dir=data_dir)
    return compile_patterns(configured or [])


def extract_issue_key(
    *candidates: str | None,
    patterns: Sequence[re.Pattern[str]],
) -> str | None:
    """候補文字列から issue_key を 1 つ抽出する。抽出できなければ None を返す。

    候補は渡された順（呼び出し側はブランチ名 → コミットメッセージの順）に評価し、
    最初にキーが見つかった候補で打ち切る。ブランチ名は命名規約で意図的に付けられる
    ため、他 issue への言及や引用が混ざりうるコミットメッセージより信頼できる。

    同一文字列内では patterns の配列順を優先順位とし、確実性の高いパターンを
    先に置くのは設定側の責務とする。1 つの文字列に複数キーが現れた場合は最左の
    もののみ採用する。複数採用すると 1 セッションが複数 issue に計上され、join
    結果が多重カウントになるため。

    一致したテキストは加工せずそのまま返す。大文字化等の正規化を行わないのは、
    キーの表記をコードが仮定しないため（正規化が必要ならパターン側で表現する）。

    patterns が空（未設定）なら常に None を返す。None や空文字（branch が NULL の
    レコード等）は候補ごとにスキップするだけで、例外にはしない。
    """
    for text in candidates:
        if not text:
            continue
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return match.group(0)
    return None


@dataclass(frozen=True)
class LinkResult:
    """突き合わせの結果。由来ごとの新規リンク件数と、未紐付けのセッション数。"""

    linked_by_source: dict[LinkSource, int]
    unlinked_sessions: int
    issue_keys_assigned: int


# 段 1: ログ内の PR 参照。ログに書かれた事実であり最も確実。
# 対象 PR が未収集でもリンクを作る。「このセッションがどの PR を出したか」は
# PR を backfill したかどうかに左右されない観測事実である。
_LINK_BY_LOG_REFERENCE = """
INSERT OR IGNORE INTO session_pr_links (session_id, repo, pr_number, link_source, linked_at)
SELECT r.session_id, r.repo, r.pr_number, ?, datetime('now')
FROM session_pr_refs r
JOIN sessions s ON s.session_id = r.session_id
"""

# 段 2: リポジトリとブランチの一致。既にリンクがあるセッションは対象外にする。
# 先行する段（より確実な由来）の結果を後続の段が上書きしないため。
_LINK_BY_REPO_BRANCH = """
INSERT OR IGNORE INTO session_pr_links (session_id, repo, pr_number, link_source, linked_at)
SELECT s.session_id, p.repo, p.pr_number, ?, datetime('now')
FROM sessions s
JOIN pull_requests p
  ON p.repo = s.repo AND p.head_branch = s.branch
WHERE s.repo IS NOT NULL
  AND s.branch IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM session_pr_links l WHERE l.session_id = s.session_id)
"""

_COUNT_UNLINKED_SESSIONS = """
SELECT COUNT(*) FROM sessions s
WHERE NOT EXISTS (SELECT 1 FROM session_pr_links l WHERE l.session_id = s.session_id)
"""


def resolve_links(
    conn: sqlite3.Connection,
    *,
    patterns: Sequence[re.Pattern[str]],
) -> LinkResult:
    """突き合わせを確実性の高い順に適用する（冪等）。

    後続の段は、先行する段で既にリンクが作られたセッションを対象にしない。
    由来の確実性の順序を保つためである（design D6）。

    チケットキーの付与はリンクの成否とは独立に行う。リンクが作れなかった
    セッションにもチケットキーは付与されうる（A-004: 片方の失敗が他方を打ち消さない）。
    """
    linked_by_source = {
        LinkSource.LOG_REFERENCE: _execute_link_stage(
            conn, _LINK_BY_LOG_REFERENCE, LinkSource.LOG_REFERENCE
        ),
        LinkSource.REPO_BRANCH: _execute_link_stage(
            conn, _LINK_BY_REPO_BRANCH, LinkSource.REPO_BRANCH
        ),
    }
    issue_keys_assigned = assign_issue_keys(conn, patterns=patterns)
    unlinked = _count(conn, _COUNT_UNLINKED_SESSIONS)
    conn.commit()
    return LinkResult(
        linked_by_source=linked_by_source,
        unlinked_sessions=unlinked,
        issue_keys_assigned=issue_keys_assigned,
    )


def _execute_link_stage(conn: sqlite3.Connection, sql: str, source: LinkSource) -> int:
    """1 段を適用し、新しく作られたリンクの件数を返す。

    INSERT OR IGNORE なので rowcount は「無視された行」を含まない。
    したがって 2 回目の実行では 0 になり、件数がそのまま「今回増えた分」になる。
    """
    cursor = conn.execute(sql, (source.value,))
    return max(cursor.rowcount, 0)


def assign_issue_keys(
    conn: sqlite3.Connection,
    *,
    patterns: Sequence[re.Pattern[str]],
) -> int:
    """sessions / pull_requests に issue_key を付与する（冪等）。更新した行数を返す。

    抽出元はブランチ名（PR 側は head_branch）とする。既に値がある行は触らない。
    collector が別の情報源から埋めた値や、以前のパターンで付いた値を壊さないため。
    パターン未設定時は何もしない（既定パターンを持たない: B-004）。
    """
    if not patterns:
        return 0
    updated = 0
    for table, source_column in (("sessions", "branch"), ("pull_requests", "head_branch")):
        rows = conn.execute(
            f"SELECT rowid, {source_column} FROM {table} WHERE issue_key IS NULL"
        ).fetchall()
        for rowid, source_value in rows:
            key = extract_issue_key(source_value, patterns=patterns)
            if key is None:
                continue
            conn.execute(f"UPDATE {table} SET issue_key = ? WHERE rowid = ?", (key, rowid))
            updated += 1
    return updated


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])
