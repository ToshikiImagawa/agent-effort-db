# agent-effort-db
Claude Code / AI エージェントの実工数を収集・蓄積するローカルDB &amp; CLI (sessions × PRs × Jira)

## セットアップ

```bash
uv sync
```

## 使い方

```bash
uv run effort-db init     # DB とテーブル/ビューを作成（冪等）
uv run effort-db relink   # 既存 sessions の issue_key を branch から再計算（冪等）
uv run effort-db stats    # issue_key の抽出率と join 率を表示（読み取り専用）
```

DB パスは以下の優先順位で解決される（環境変数 > `config.toml` > デフォルト）:

1. 環境変数 `EFFORT_DB_PATH`
2. `~/.claude/plugins/data/effort-db/config.toml` の `db_path`
3. デフォルト: `~/.claude/plugins/data/effort-db/effort.db`

`backfill` / `collect-session` / `query` は未実装（後続issueで対応）。

### issue_key の抽出パターン

**既定パターンは持たない**（`CONSTITUTION.md` B-004: チケットキーの形式をコードに書かない）。
`config.toml` で指定するまで `issue_key` は付与されず、`relink` は何もしない:

```toml
issue_key_patterns = ["(?<![0-9A-Za-z])PROJ-[0-9]+(?![0-9A-Za-z])"]
```

- 配列の順序が優先順位（先に一致したパターンを採用）。確実性の高いパターンを先に置く
- 1 つの文字列に複数キーがある場合は最左のみ採用
- 一致したテキストは加工せず保存する（大文字化等の正規化はしないので、表記を揃える必要があればパターン側で表現する）
- 抽出できないレコードは捨てず `issue_key = NULL`（unlinked）で保持する

`stats` の join 率が低い場合は、パターンの複雑化よりも先にブランチ命名規約の見直しを検討する。

## 開発

```bash
uv run ruff check .           # lint
uv run ruff format .          # format
uv run mypy src/effort_db     # 型チェック
uv run pytest                 # テスト
```
