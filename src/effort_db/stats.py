"""収集の健全性の集計（読み取り専用）。

**join 率はキー種別ごとに出す。** 低い場合に抽出ロジックを複雑にするのではなく、
どのキーが効いていないかを見て、より確実なキーの採用や命名規約の見直しを判断する
ための材料にする（design D10）。

`stats` が DB を書き換えないのは、join 率の変化がデータ収集由来かコマンド実行由来か
区別できなくなるのを避けるため。突き合わせは `link` の責務である。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from effort_db.linker import LinkSource

# join 率がこれを下回ったら警告する。ブランチ命名規約や収集範囲の見直しを促す閾値。
LOW_LINK_RATE_THRESHOLD = 50.0

# 由来として永続化される値（UNLINKED はリンクの不在を表す論理値なので除く）。
_PERSISTED_SOURCES = (LinkSource.LOG_REFERENCE, LinkSource.REPO_BRANCH)


@dataclass(frozen=True)
class Stats:
    """収集の健全性。率は 0 件でも落ちないようプロパティ側で算出する。"""

    sessions: int
    pull_requests: int
    prs_with_head_branch: int
    links_by_source: dict[LinkSource, int]
    linked_sessions_by_source: dict[LinkSource, int]
    links_with_unknown_source: int
    unlinked_sessions: int
    sessions_without_repo_or_branch: int
    sessions_in_repos_with_prs: int
    linked_sessions_in_repos_with_prs: int
    sessions_with_issue_key: int
    prs_with_issue_key: int
    issue_keys: int

    @property
    def join_rate_by_source(self) -> dict[LinkSource, float]:
        """由来ごとの join 率（%）。分母はセッション総数。

        段は先行する段の結果を上書きしないため、1 セッションは 1 つの由来にしか
        属さない。したがって各由来の率と未紐付けの率の合計は 100% になる。
        """
        rates = {
            source: _rate(self.linked_sessions_by_source.get(source, 0), self.sessions)
            for source in _PERSISTED_SOURCES
        }
        rates[LinkSource.UNLINKED] = _rate(self.unlinked_sessions, self.sessions)
        return rates

    @property
    def focused_join_rate(self) -> float:
        """PR を収集済みのリポジトリに属するセッションだけを分母にした join 率（%）。

        PR を取り込んでいないリポジトリのセッションは構造的に紐付けられない。
        全体の率だけを見ると、突き合わせの不備と PR 未収集を区別できない。
        """
        return _rate(self.linked_sessions_in_repos_with_prs, self.sessions_in_repos_with_prs)

    @property
    def sessions_issue_key_rate(self) -> float:
        return _rate(self.sessions_with_issue_key, self.sessions)

    @property
    def prs_issue_key_rate(self) -> float:
        return _rate(self.prs_with_issue_key, self.pull_requests)


def collect_stats(conn: sqlite3.Connection) -> Stats:
    """収集件数・由来ごとの join 率・未紐付け件数を集計する（読み取り専用）。"""
    links_by_source, linked_sessions_by_source, unknown = _collect_link_counts(conn)
    return Stats(
        sessions=_count(conn, "SELECT COUNT(*) FROM sessions"),
        pull_requests=_count(conn, "SELECT COUNT(*) FROM pull_requests"),
        prs_with_head_branch=_count(
            conn, "SELECT COUNT(*) FROM pull_requests WHERE head_branch IS NOT NULL"
        ),
        links_by_source=links_by_source,
        linked_sessions_by_source=linked_sessions_by_source,
        links_with_unknown_source=unknown,
        unlinked_sessions=_count(
            conn,
            "SELECT COUNT(*) FROM sessions s WHERE NOT EXISTS ("
            " SELECT 1 FROM session_pr_links l WHERE l.session_id = s.session_id)",
        ),
        # repo か branch が欠けているセッションは (repo, branch) では紐付けられない。
        # join 率が低いときに「抽出の問題」と「そもそもキーが無い」を切り分ける。
        sessions_without_repo_or_branch=_count(
            conn, "SELECT COUNT(*) FROM sessions WHERE repo IS NULL OR branch IS NULL"
        ),
        sessions_in_repos_with_prs=_count(
            conn,
            "SELECT COUNT(*) FROM sessions WHERE repo IN (SELECT DISTINCT repo FROM pull_requests)",
        ),
        linked_sessions_in_repos_with_prs=_count(
            conn,
            "SELECT COUNT(*) FROM sessions s"
            " WHERE s.repo IN (SELECT DISTINCT repo FROM pull_requests)"
            " AND EXISTS (SELECT 1 FROM session_pr_links l WHERE l.session_id = s.session_id)",
        ),
        sessions_with_issue_key=_count(
            conn, "SELECT COUNT(*) FROM sessions WHERE issue_key IS NOT NULL"
        ),
        prs_with_issue_key=_count(
            conn, "SELECT COUNT(*) FROM pull_requests WHERE issue_key IS NOT NULL"
        ),
        issue_keys=_count(conn, "SELECT COUNT(*) FROM effort_by_issue"),
    )


def _collect_link_counts(
    conn: sqlite3.Connection,
) -> tuple[dict[LinkSource, int], dict[LinkSource, int], int]:
    """由来ごとのリンク行数と、紐付いたセッション数を集計する。

    知らない由来の値（将来のバージョンが書いた値）で落とさない。件数だけ別に数え、
    内訳が全体と合わないことが分かるようにする。
    """
    links: dict[LinkSource, int] = dict.fromkeys(_PERSISTED_SOURCES, 0)
    sessions: dict[LinkSource, int] = dict.fromkeys(_PERSISTED_SOURCES, 0)
    unknown = 0
    rows = conn.execute(
        "SELECT link_source, COUNT(*), COUNT(DISTINCT session_id)"
        " FROM session_pr_links GROUP BY link_source"
    ).fetchall()
    for raw_source, link_count, session_count in rows:
        try:
            source = LinkSource(raw_source)
        except ValueError:
            unknown += int(link_count)
            continue
        links[source] = int(link_count)
        sessions[source] = int(session_count)
    return links, sessions, unknown


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def _rate(part: int, total: int) -> float:
    """割合(%)を返す。空 DB でゼロ除算しないよう total == 0 は 0.0% として扱う。"""
    if total == 0:
        return 0.0
    return part / total * 100
