# agent-effort-db
Claude Code / AI エージェントの実工数を収集・蓄積するローカルDB &amp; CLI (sessions × PRs × Jira)

## セットアップ

```bash
uv sync
```

## 使い方

```bash
uv run effort-db init                            # DB とテーブル/ビューを作成（冪等）
uv run effort-db backfill sessions               # 過去のセッションログを一括取り込み（冪等）
uv run effort-db backfill prs --repo owner/repo  # gh 経由でマージ済み PR を取り込み（冪等）
uv run effort-db collect-session <session-id>    # 単一セッションのみ取り込み（冪等）
uv run effort-db link                            # セッションと PR の突き合わせ（冪等）
uv run effort-db stats                           # 由来ごとの join 率を表示（読み取り専用）
uv run effort-db query "SELECT * FROM effort_by_branch LIMIT 10"   # 任意の SQL（読み取り専用）
```

DB パスは以下の優先順位で解決される（環境変数 > `config.toml` > デフォルト）:

1. 環境変数 `EFFORT_DB_PATH`
2. `~/.claude/plugins/data/effort-db/config.toml` の `db_path`
3. デフォルト: `~/.claude/plugins/data/effort-db/effort.db`

### 突き合わせ（`link`）

収集とは独立したコマンドである。PR を後から取り込んだ場合に、セッションを再収集せずに
リンクだけを作り直せるようにするため。確実性の高い順に段階適用し、**どのキーで紐付いたか**を
`session_pr_links.link_source` に残す:

| 由来              | 内容                                | 備考                             |
|:----------------|:----------------------------------|:-------------------------------|
| `log_reference` | セッションログ内の PR 参照                   | 最優先。対象 PR が未収集でもリンクを作る         |
| `repo_branch`   | `(repo, branch)` が PR の head ブランチと一致 | 既にリンクがあるセッションには適用しない           |
| （補助）`issue_key` | チケットキーの付与                         | 単独では join キーにしない（実データで 1 件も紐付かない） |

紐付かなかったセッションは捨てず、リンクが無い状態（`unlinked`）で保持する。

### 参照（`query` / ビュー）

集約はビューで提供する（raw 層は潰さない）:

- `effort_by_branch`: `(repo, branch)` 単位
- `effort_by_issue`: `issue_key` が付与された分のみ

`query` は読み取り専用（`PRAGMA query_only`）。逃げ道としての SQL 実行で収集済みデータを
壊せないようにしてある。NULL は `NULL` と表示され、0 と区別できる。

### issue_key の抽出パターン

**既定パターンは持たない**（`CONSTITUTION.md` B-004: チケットキーの形式をコードに書かない）。
`config.toml` で指定するまで `issue_key` は付与されない（`link` の突き合わせ自体は動く）:

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
