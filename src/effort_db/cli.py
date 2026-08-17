"""effort-db CLIエントリポイント。"""

from __future__ import annotations

import re
import sqlite3

import typer

from effort_db import config, linker, schema
from effort_db import stats as stats_module
from effort_db.collectors import github, session

app = typer.Typer(help="Claude Code / AIエージェントの実工数を収集・蓄積するCLI")
backfill_app = typer.Typer(help="過去データの一括取り込み")
app.add_typer(backfill_app, name="backfill")


@app.command()
def init() -> None:
    """DBとテーブル/ビューを作成する（冪等）。"""
    db_path = config.resolve_db_path()
    conn = sqlite3.connect(db_path)
    try:
        schema.init_db(conn)
    finally:
        conn.close()
    typer.echo(f"DB initialized: {db_path}")


@backfill_app.command("sessions")
def backfill_sessions() -> None:
    """過去jsonlを全走査して sessions に取り込む（冪等）。"""
    db_path = config.resolve_db_path()
    conn = sqlite3.connect(db_path)
    try:
        schema.init_db(conn)  # DDLは冪等なので init 未実行でも動くようにする
        count = session.collect_all(conn)
    finally:
        conn.close()
    typer.echo(f"collected {count} sessions: {db_path}")


@backfill_app.command("prs")
def backfill_prs(
    repo: str = typer.Option(..., "--repo", help="対象リポジトリ (owner/repo)"),
    limit: int = typer.Option(
        github.DEFAULT_LIMIT, "--limit", min=1, help="ghから取得するマージ済みPRの最大件数"
    ),
) -> None:
    """gh経由でマージ済みPRを取得し pull_requests に upsert する（冪等）。"""
    try:
        rows = github.fetch_merged_prs(repo, limit=limit)
    except github.GhError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    db_path = config.resolve_db_path()
    conn = sqlite3.connect(db_path)
    try:
        # init 未実行でも動くようにテーブル作成を通す（init_db は冪等）。
        schema.init_db(conn)
        count = github.upsert_pull_requests(conn, rows)
    finally:
        conn.close()

    typer.echo(f"{count} PRs upserted from {repo}")
    if len(rows) >= limit:
        # ちょうど limit 件だった場合と打ち切られた場合は区別できないため、断定せず警告する。
        typer.echo(
            f"警告: 取得件数が --limit {limit} に達しました。これより古いPRは取得できて"
            "いない可能性があります。全件必要な場合は --limit を増やして再実行してください。",
            err=True,
        )


@app.command("collect-session")
def collect_session(session_id: str) -> None:
    """単一セッションを取り込む（インクリメンタル収集用・冪等）。"""
    db_path = config.resolve_db_path()
    conn = sqlite3.connect(db_path)
    try:
        schema.init_db(conn)
        collected = session.collect_one(conn, session_id)
    finally:
        conn.close()
    if not collected:
        typer.echo(f"セッションログが見つかりません: {session_id}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"collected session: {session_id}")


def _connect_initialized_db() -> sqlite3.Connection:
    """init 済みDBに接続する。未作成/未初期化ならトレースバックではなく案内を出す。"""
    db_path = config.resolve_db_path()
    if not db_path.is_file():
        typer.echo(f"DB がありません: 先に effort-db init を実行してください ({db_path})", err=True)
        raise typer.Exit(code=1)

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"sessions", "pull_requests"} <= tables:
        conn.close()
        typer.echo(f"DB が未初期化です: effort-db init を実行してください ({db_path})", err=True)
        raise typer.Exit(code=1)
    return conn


def _resolve_patterns_or_exit() -> list[re.Pattern[str]]:
    """設定のパターンを解決する。設定不備はトレースバックにせず案内にする。"""
    try:
        return linker.resolve_patterns()
    except ValueError as exc:
        typer.echo(f"設定エラー: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def link() -> None:
    """セッションと PR の突き合わせを段階適用する（冪等）。

    収集とは独立したコマンドにしてある。PR を後から取り込んだ場合に、
    セッションを再収集せずにリンクだけを作り直せるようにするため。
    """
    patterns = _resolve_patterns_or_exit()
    conn = _connect_initialized_db()
    try:
        result = linker.resolve_links(conn, patterns=patterns)
    finally:
        conn.close()

    for source, count in result.linked_by_source.items():
        typer.echo(f"{source.value:<14}: {count} 件のリンクを追加")
    typer.echo(f"issue_key     : {result.issue_keys_assigned} 行に付与")
    typer.echo(f"unlinked      : {result.unlinked_sessions} セッション")
    if not patterns:
        typer.echo(_no_patterns_warning())


@app.command()
def stats() -> None:
    """収集件数・由来ごとの join 率・未紐付け件数を表示する（読み取り専用）。"""
    patterns = _resolve_patterns_or_exit()
    conn = _connect_initialized_db()
    try:
        collected = stats_module.collect_stats(conn)
    finally:
        conn.close()

    typer.echo(f"sessions      : {collected.sessions} 行")
    typer.echo(
        f"pull_requests : {collected.pull_requests} 行"
        f" (head_branch 有り {collected.prs_with_head_branch} 行)"
    )
    typer.echo("join 率（由来ごと。分母は sessions 総数）:")
    for source, rate in collected.join_rate_by_source.items():
        count = (
            collected.unlinked_sessions
            if source is linker.LinkSource.UNLINKED
            else collected.linked_sessions_by_source.get(source, 0)
        )
        typer.echo(f"  {source.value:<14}: {count} セッション ({rate:.1f}%)")
    typer.echo(
        f"PR 収集済みリポジトリに限った join 率: {collected.focused_join_rate:.1f}%"
        f" ({collected.linked_sessions_in_repos_with_prs}/{collected.sessions_in_repos_with_prs})"
    )
    typer.echo(
        f"issue_key     : sessions {collected.sessions_with_issue_key} 行"
        f" ({collected.sessions_issue_key_rate:.1f}%) / PR {collected.prs_with_issue_key} 行"
        f" ({collected.prs_issue_key_rate:.1f}%) / {collected.issue_keys} 種類"
    )

    for warning in _stats_warnings(collected, patterns_configured=bool(patterns)):
        typer.echo(warning)


def _no_patterns_warning() -> str:
    return (
        f"[警告] config.toml の {config.CONFIG_KEY_ISSUE_KEY_PATTERNS} が未設定のため "
        "issue_key は付与されない（既定パターンは持たない）。"
    )


def _stats_warnings(collected: stats_module.Stats, *, patterns_configured: bool) -> list[str]:
    """join 率が低いことを見落とさないよう、閾値割れと構造的な原因を明示する行を返す。

    0 行のときは警告しない（空DBに見直しを促しても判断材料にならない）。
    原因が特定できる場合は率の低さを重ねて警告しない（誤誘導になる）。
    """
    warnings: list[str] = []
    threshold = stats_module.LOW_LINK_RATE_THRESHOLD
    # パターン未設定は収集内容によらない設定の問題なので、空DBでも伝える。
    if not patterns_configured:
        warnings.append(_no_patterns_warning())
    if collected.sessions == 0:
        return warnings
    if collected.pull_requests == 0:
        warnings.append(
            "[警告] PR が 1 件も収集されていないため (repo, branch) では紐付かない。"
            "先に effort-db backfill prs --repo <owner/repo> を実行すること。"
        )
    elif collected.focused_join_rate < threshold:
        warnings.append(
            f"[警告] PR 収集済みリポジトリでの join 率が低い "
            f"({collected.focused_join_rate:.1f}% < {threshold:.0f}%)。"
            "ブランチ命名規約か PR の収集範囲（--limit / 対象リポジトリ）を見直すこと。"
        )
    if collected.sessions_without_repo_or_branch:
        warnings.append(
            f"[情報] repo または branch が欠けたセッションが "
            f"{collected.sessions_without_repo_or_branch} 行ある。"
            "これらは (repo, branch) では構造的に紐付けられない。"
        )
    if collected.links_with_unknown_source:
        warnings.append(
            f"[警告] 未知の由来を持つリンクが {collected.links_with_unknown_source} 行ある。"
            "新しいバージョンで書かれた DB の可能性がある（内訳が全体と合わない）。"
        )
    return warnings


@app.command()
def query(sql: str) -> None:
    """任意の SQL で参照する（読み取り専用）。

    書き込みを許さないのは、逃げ道としての SQL 実行で収集済みデータを壊せる状態に
    しないため（raw 層を失わない: A-002）。書き換えが必要なら sqlite3 を直接使う。
    """
    conn = _connect_initialized_db()
    try:
        conn.execute("PRAGMA query_only = ON")
        try:
            cursor = conn.execute(sql)
        except sqlite3.Error as exc:
            typer.echo(f"SQL エラー: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        if cursor.description is None:
            return
        typer.echo("\t".join(column[0] for column in cursor.description))
        for row in cursor:
            typer.echo("\t".join(_format_cell(value) for value in row))
    finally:
        conn.close()


def _format_cell(value: object) -> str:
    """NULL を空文字にしない。0 と NULL の区別が出力でも失われないようにする。"""
    return "NULL" if value is None else str(value)
