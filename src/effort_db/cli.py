"""effort-db CLIエントリポイント。"""

from __future__ import annotations

import sqlite3

import typer

from effort_db import config, schema

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
    """過去jsonl走査（未実装）。"""
    _not_implemented("backfill sessions")


@backfill_app.command("prs")
def backfill_prs(
    repo: str = typer.Option(..., "--repo", help="対象リポジトリ (owner/repo)"),
) -> None:
    """gh経由でPR取得（未実装）。"""
    _not_implemented("backfill prs")


@app.command("collect-session")
def collect_session(session_id: str) -> None:
    """インクリメンタル収集用（未実装）。"""
    _not_implemented("collect-session")


@app.command()
def stats() -> None:
    """join率などの健全性確認（未実装）。"""
    _not_implemented("stats")


@app.command()
def query(sql: str) -> None:
    """素のSQL逃げ道（未実装）。"""
    _not_implemented("query")
