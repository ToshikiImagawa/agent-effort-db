"""effort-db CLIエントリポイント。"""

from __future__ import annotations

import sqlite3

import typer

from effort_db import config, schema
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


@app.command()
def stats() -> None:
    """join率などの健全性確認（未実装）。"""
    _not_implemented("stats")


@app.command()
def query(sql: str) -> None:
    """素のSQL逃げ道（未実装）。"""
    _not_implemented("query")
