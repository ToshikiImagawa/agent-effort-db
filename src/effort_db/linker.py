"""issue_key の抽出と、突き合わせ（join）健全性の集計。

issue_key は sessions と pull_requests を join する唯一のキーである。抽出できな
かったレコードは捨てずに issue_key = NULL（unlinked）のまま保持する。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from effort_db import config

# sessions 側の抽出率がこれを下回ったら stats で警告する。ブランチ命名規約の
# 見直しを促す閾値であり、半分以上取れていないなら規約側の問題とみなす。
LOW_LINK_RATE_THRESHOLD = 50.0


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
class RelinkResult:
    """relink_sessions の結果。"""

    scanned: int
    updated: int
    unlinked: int


def relink_sessions(
    conn: sqlite3.Connection,
    *,
    patterns: Sequence[re.Pattern[str]],
) -> RelinkResult:
    """既存 sessions の issue_key を branch から再計算する（冪等）。

    stats 実行時の自動再計算ではなく専用経路にしたのは、stats を読み取り専用の
    診断コマンドに保つため。実行するたびに DB が書き換わると、join 率の変化が
    データ収集由来かコマンド実行由来か区別できなくなる。

    抽出できなかった行の既存 issue_key は消さない。collector がブランチ名以外
    （コミットメッセージ等）から埋めた値を壊さないため。同じ branch と同じ
    パターンなら結果は変わらないので冪等である。

    pull_requests を対象にしないのは、同テーブルにキーの抽出元となるテキスト列
    （タイトル / ブランチ名）が無く、収集時に埋めるしかないため。
    """
    rows = conn.execute("SELECT session_id, branch, issue_key FROM sessions").fetchall()
    updated = 0
    for session_id, branch, current in rows:
        extracted = extract_issue_key(branch, patterns=patterns)
        if extracted is None or extracted == current:
            continue
        conn.execute(
            "UPDATE sessions SET issue_key = ? WHERE session_id = ?",
            (extracted, session_id),
        )
        updated += 1
    conn.commit()
    return RelinkResult(
        scanned=len(rows),
        updated=updated,
        unlinked=_count(conn, "SELECT COUNT(*) FROM sessions WHERE issue_key IS NULL"),
    )


@dataclass(frozen=True)
class LinkStats:
    """join の健全性を表す件数。率は 0 件でも落ちないようプロパティ側で算出する。"""

    sessions_total: int
    sessions_linked: int
    prs_total: int
    prs_linked: int
    task_effort_keys: int
    joined_keys: int
    sessions_only_keys: int
    prs_only_keys: int

    @property
    def sessions_link_rate(self) -> float:
        return _rate(self.sessions_linked, self.sessions_total)

    @property
    def prs_link_rate(self) -> float:
        return _rate(self.prs_linked, self.prs_total)


def collect_link_stats(conn: sqlite3.Connection) -> LinkStats:
    """sessions / pull_requests の issue_key 充填状況と join 状況を集計する（読み取り専用）。"""
    return LinkStats(
        sessions_total=_count(conn, "SELECT COUNT(*) FROM sessions"),
        sessions_linked=_count(conn, "SELECT COUNT(*) FROM sessions WHERE issue_key IS NOT NULL"),
        prs_total=_count(conn, "SELECT COUNT(*) FROM pull_requests"),
        prs_linked=_count(conn, "SELECT COUNT(*) FROM pull_requests WHERE issue_key IS NOT NULL"),
        # task_effort は issue_key で GROUP BY するビューなので、NULL 以外の行数が
        # 参照側から見える課題の数になる。
        task_effort_keys=_count(
            conn, "SELECT COUNT(*) FROM task_effort WHERE issue_key IS NOT NULL"
        ),
        joined_keys=_count(conn, _set_op_count_sql("INTERSECT", "sessions", "pull_requests")),
        sessions_only_keys=_count(conn, _set_op_count_sql("EXCEPT", "sessions", "pull_requests")),
        prs_only_keys=_count(conn, _set_op_count_sql("EXCEPT", "pull_requests", "sessions")),
    )


def _set_op_count_sql(operator: str, left: str, right: str) -> str:
    return (
        "SELECT COUNT(*) FROM ("
        f"SELECT issue_key FROM {left} WHERE issue_key IS NOT NULL"
        f" {operator} "
        f"SELECT issue_key FROM {right} WHERE issue_key IS NOT NULL"
        ")"
    )


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def _rate(part: int, total: int) -> float:
    """割合(%)を返す。空 DB でゼロ除算しないよう total == 0 は 0.0% として扱う。"""
    if total == 0:
        return 0.0
    return part / total * 100
