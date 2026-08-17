"""gh CLI 経由の PR 収集。

PyGithub を使わず `gh` の subprocess 呼び出しにしているのは、認証を `gh auth` に
委譲するため（トークンを本ツールで持たない）。

PR 本文・レビュー本文は取得しても DB に保存しない。`pull_requests` に本文列は無く、
追加もしない（本リポジトリは PUBLIC のため）。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_LIMIT = 50

# headRefName は突き合わせの主軸（(repo, branch) の一致）に使うため必須。
# これが無いとセッションと PR を結び付ける手段が無くなる。
_JSON_FIELDS = (
    "number,headRefName,additions,deletions,changedFiles,createdAt,mergedAt,labels,reviews"
)

_AUTH_HINT = (
    "gh の active account が意図したアカウントか確認してください"
    "（`gh auth status` / `gh auth switch`、または `GH_TOKEN` を明示指定）。"
    "Enterprise Managed User と個人アカウントが両方登録されている場合、"
    "読み取りは通るのに特定リポジトリだけ権限不足になることがあります。"
)

# 権限の無いリポジトリは存在しないものとして返るため、リポジトリ名の誤りと同じ文言になる。
# 「名前が間違っているように見えるが実はアカウント違い」を取り違えないようヒントを出す。
_AUTH_FAILURE_MARKERS = (
    "unauthorized",
    "enterprise managed user",
    "could not resolve to a repository",
)


class GhError(RuntimeError):
    """gh コマンドの実行または出力の解釈に失敗したことを表す。"""


# gh コマンド引数を受け取り stdout を返す呼び出し口。テストからスタブを差し込むための型。
GhRunner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class PullRequestRow:
    """pull_requests テーブル 1 行分。issue_key は linker の担当なので持たない。"""

    repo: str
    pr_number: int
    head_branch: str | None
    additions: int
    deletions: int
    changed_files: int
    review_rounds: int
    created_at: str | None
    merged_at: str | None
    labels: str


def run_gh(args: Sequence[str]) -> str:
    """gh を実行して stdout を返す。非 0 終了時は exit code と stderr を添えて失敗させる。"""
    if shutil.which("gh") is None:
        raise GhError("gh コマンドが見つかりません。GitHub CLI をインストールしてください。")

    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "(stderr は空)"
        message = f"gh {' '.join(args)} が exit code {proc.returncode} で失敗しました: {stderr}"
        if _looks_like_auth_failure(stderr):
            message = f"{message}\n{_AUTH_HINT}"
        raise GhError(message)
    return proc.stdout


def _looks_like_auth_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


def fetch_merged_prs(
    repo: str,
    *,
    limit: int = DEFAULT_LIMIT,
    runner: GhRunner | None = None,
) -> list[PullRequestRow]:
    """指定リポジトリのマージ済み PR を最大 limit 件取得する。

    並び順は `gh pr list` の既定である「作成日の新しい順」であり、マージ日順ではない。
    そのため limit で打ち切ると「作成は古いが最近マージされた PR」が落ちる。全件必要な
    場合は limit を履歴全体より大きくすること。

    空配列は「マージ済み PR が 0 件」として正常扱いする（権限不足は runner 側で
    非 0 終了として検出される）。

    runner の既定値を引数のデフォルトに束縛せず None にしているのは、テストから
    `run_gh` を差し替えた場合にそれが効くようにするため。
    """
    run = runner if runner is not None else run_gh
    stdout = run(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--json",
            _JSON_FIELDS,
            "--limit",
            str(limit),
        ]
    )
    prs = _parse_pr_list(stdout)
    try:
        return [_to_row(repo, pr) for pr in prs]
    except (KeyError, TypeError, ValueError) as exc:
        # gh のフィールド名変更等で欠損した場合も、生の traceback ではなく GhError で失敗させる。
        raise GhError(f"gh の出力から PR 情報を取り出せませんでした: {exc!r}") from exc


def _parse_pr_list(stdout: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise GhError(f"gh の出力を JSON として解釈できませんでした: {exc}") from exc
    if not isinstance(payload, list):
        raise GhError(f"gh の出力が配列ではありません: {type(payload).__name__}")
    return payload


def _to_row(repo: str, pr: dict[str, Any]) -> PullRequestRow:
    head_branch = pr.get("headRefName")
    return PullRequestRow(
        repo=repo,
        pr_number=int(pr["number"]),
        # 空文字は「取れなかった」と同じ扱いにする。突き合わせで空文字同士が
        # 一致してしまうと、無関係なセッションと PR が結び付く。
        head_branch=head_branch if isinstance(head_branch, str) and head_branch else None,
        additions=int(pr.get("additions") or 0),
        deletions=int(pr.get("deletions") or 0),
        changed_files=int(pr.get("changedFiles") or 0),
        review_rounds=_count_review_rounds(pr.get("reviews") or []),
        created_at=pr.get("createdAt"),
        merged_at=pr.get("mergedAt"),
        labels=json.dumps(
            [label["name"] for label in pr.get("labels") or []],
            ensure_ascii=False,
        ),
    )


def _count_review_rounds(reviews: Sequence[dict[str, Any]]) -> int:
    """レビューの往復回数を数える。

    「往復」= 指摘を受けて作者が直し、再度レビューに出したサイクル、と定義し、
    state が CHANGES_REQUESTED のレビュー件数で数える。APPROVED / COMMENTED は
    やり直しを強制しないため数えない（承認だけで通った PR は 0 になる）。
    工数指標として見たいのは「何回やり直したか」であり、レビューが付いた回数では
    ないため、この定義を採る。
    """
    return sum(1 for review in reviews if review.get("state") == "CHANGES_REQUESTED")


def upsert_pull_requests(conn: sqlite3.Connection, rows: Sequence[PullRequestRow]) -> int:
    """(repo, pr_number) を一意キーに upsert する。issue_key は既存値を保持する。"""
    conn.executemany(
        """
        INSERT INTO pull_requests (
          repo, pr_number, head_branch, additions, deletions, changed_files,
          review_rounds, created_at, merged_at, labels, collected_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT (repo, pr_number) DO UPDATE SET
          head_branch   = excluded.head_branch,
          additions     = excluded.additions,
          deletions     = excluded.deletions,
          changed_files = excluded.changed_files,
          review_rounds = excluded.review_rounds,
          created_at    = excluded.created_at,
          merged_at     = excluded.merged_at,
          labels        = excluded.labels,
          collected_at  = datetime('now')
        """,
        [
            (
                row.repo,
                row.pr_number,
                row.head_branch,
                row.additions,
                row.deletions,
                row.changed_files,
                row.review_rounds,
                row.created_at,
                row.merged_at,
                row.labels,
            )
            for row in rows
        ],
    )
    conn.commit()
    return len(rows)
