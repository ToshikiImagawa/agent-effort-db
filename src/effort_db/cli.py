"""effort-db CLIエントリポイント。"""

from __future__ import annotations

import re
import sqlite3

import typer

from effort_db import config, linker, schema
from effort_db.collectors import session

app = typer.Typer(help="Claude Code / AIエージェントの実工数を収集・蓄積するCLI")
backfill_app = typer.Typer(help="過去データの一括取り込み（未実装）")
app.add_typer(backfill_app, name="backfill")

_NOT_IMPLEMENTED_MESSAGE = "未実装: {name} は別issueで実装予定です。"


def _not_implemented(name: str) -> None:
    typer.echo(_NOT_IMPLEMENTED_MESSAGE.format(name=name), err=True)
    raise typer.Exit(code=1)


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
) -> None:
    """gh経由でPR取得（未実装）。"""
    _not_implemented("backfill prs")


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
def stats() -> None:
    """issue_key の抽出率とjoin率を表示する（読み取り専用）。"""
    patterns = _resolve_patterns_or_exit()
    conn = _connect_initialized_db()
    try:
        link_stats = linker.collect_link_stats(conn)
    finally:
        conn.close()

    typer.echo(
        f"sessions      : {_format_linked(link_stats.sessions_linked, link_stats.sessions_total)}"
    )
    typer.echo(f"pull_requests : {_format_linked(link_stats.prs_linked, link_stats.prs_total)}")
    typer.echo(f"task_effort   : {link_stats.task_effort_keys} issue_key")
    typer.echo(f"join 済み     : {link_stats.joined_keys} issue_key (sessions と PR の両方に存在)")
    typer.echo(f"sessions のみ : {link_stats.sessions_only_keys} issue_key")
    typer.echo(f"PR のみ       : {link_stats.prs_only_keys} issue_key")

    for warning in _link_warnings(link_stats, patterns_configured=bool(patterns)):
        typer.echo(warning)


@app.command()
def relink() -> None:
    """既存 sessions の issue_key を branch から再計算する（冪等）。"""
    patterns = _resolve_patterns_or_exit()
    if not patterns:
        typer.echo(
            f"config.toml の {config.CONFIG_KEY_ISSUE_KEY_PATTERNS} が未設定のため何もしない。"
        )
        return

    conn = _connect_initialized_db()
    try:
        result = linker.relink_sessions(conn, patterns=patterns)
    finally:
        conn.close()
    typer.echo(
        f"relink: {result.scanned} 行を走査, {result.updated} 行を更新, "
        f"{result.unlinked} 行が unlinked (issue_key IS NULL)"
    )


def _format_linked(linked: int, total: int) -> str:
    rate = 0.0 if total == 0 else linked / total * 100
    return f"{total} 行, issue_key 有り {linked} 行 ({rate:.1f}%)"


def _link_warnings(link_stats: linker.LinkStats, *, patterns_configured: bool) -> list[str]:
    """join率が低いことを見落とさないよう、閾値割れを明示する行を返す。

    0 行のときは警告しない（空DBに命名規約の見直しを促しても判断材料にならない）。
    パターン未設定時は抽出率の低さを重ねて警告しない（原因が設定未了と特定できており、
    命名規約の見直しを促すのは誤誘導になる）。
    """
    warnings: list[str] = []
    threshold = linker.LOW_LINK_RATE_THRESHOLD
    if not patterns_configured:
        return [
            f"[警告] config.toml の {config.CONFIG_KEY_ISSUE_KEY_PATTERNS} が未設定のため "
            "issue_key は付与されない（既定パターンは持たない）。"
        ]
    if link_stats.sessions_total > 0 and link_stats.sessions_link_rate < threshold:
        warnings.append(
            f"[警告] sessions の issue_key 抽出率が低い ({link_stats.sessions_link_rate:.1f}% "
            f"< {threshold:.0f}%)。ブランチ命名規約か config.toml の "
            f"{config.CONFIG_KEY_ISSUE_KEY_PATTERNS} を見直すこと。"
        )
    if link_stats.prs_total > 0 and link_stats.prs_link_rate < threshold:
        warnings.append(
            f"[警告] pull_requests の issue_key 抽出率が低い ({link_stats.prs_link_rate:.1f}% "
            f"< {threshold:.0f}%)。"
        )
    if link_stats.joined_keys == 0 and (link_stats.sessions_linked or link_stats.prs_linked):
        warnings.append(
            "[警告] join が 1 件も成立していない。sessions と PR で issue_key の表記が"
            "揃っているか確認すること。"
        )
    return warnings


@app.command()
def query(sql: str) -> None:
    """素のSQL逃げ道（未実装）。"""
    _not_implemented("query")
