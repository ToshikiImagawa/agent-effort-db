# agent-effort-db
Claude Code / AI エージェントの実工数を収集・蓄積するローカルDB &amp; CLI (sessions × PRs × Jira)

## セットアップ

```bash
uv sync
```

## 使い方

```bash
uv run effort-db init   # DB とテーブル/ビューを作成（冪等）
```

DB パスは以下の優先順位で解決される（環境変数 > `config.toml` > デフォルト）:

1. 環境変数 `EFFORT_DB_PATH`
2. `~/.claude/plugins/data/effort-db/config.toml` の `db_path`
3. デフォルト: `~/.claude/plugins/data/effort-db/effort.db`

`backfill` / `collect-session` / `stats` / `query` は未実装（後続issueで対応）。

## 開発

```bash
uv run ruff check .           # lint
uv run ruff format .          # format
uv run mypy src/effort_db     # 型チェック
uv run pytest                 # テスト
```
