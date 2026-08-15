# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

Claude Code / AI エージェントの実工数を収集・蓄積するローカルDB & CLI（sessions × PRs × Jira）。

Claude Code が出す工数見積もりは「人間が実施する前提」で大きく外れることが多い。そこで**実際にかかった工数を実測データから蓄積し、見積もりの指標（reference class）として使えるDB** を作ることが目的。

現時点では `src/effort_db/` 等のパッケージ骨格・実装コードは未着手（README.md / .gitignore / `.sdd/` / `.claude/` のみ存在）。以降のセクションは決定済みの設計方針であり、実装時はこれに従うこと。

## 設計判断の背景

- GitHub の issue/PR の経過時間はレビュー待ち・放置を含みノイジー → 補助的な特徴量として使う
- **本命は Claude Code 自身のセッションログ**（`~/.claude/projects/**/*.jsonl`）。ターン数・ツール呼び出し回数・実経過時間が取れる
- 見積もり単位は「人日」ではなく「セッション数 / ターン数 / 実時間」などエージェントネイティブな単位とし、点推定ではなく分布（中央値/p90）で扱う
- セッション ↔ PR ↔ Jira の join キーはブランチ名等に含まれるチケットキー（例: `ABC-123`）。紐付かないレコードは捨てず `unlinked` として保持する

## アーキテクチャ方針（決定済み）

- 「収集」と「参照」を分離する。skill / MCP / hook / プラグインは全て後付けのビューであり、先にDBとCLIを作る
- 独立リポジトリの CLI ツールとして実装する（単発スクリプトやプラグインリポジトリ同居は却下済み）
  - 理由: DBの寿命はプラグインより長い / CLIなら hook・cron・MCP どこからでも同じコマンドを呼べる / チーム配布単位として自然
- 将来の分岐は全てCLIの外側で吸収する:
  - hook収集 → hookからCLIを1コマンド叩く
  - プラグイン化 → このリポジトリに plugin.json と skill を追加
  - MCP化 → CLI関数をimportする薄い `mcp.py` を追加
  - チームDB化 → ストレージ層の接続先だけ差し替え

## 絶対ルール（データ・公開範囲）

- **実セッションログ（`~/.claude/projects/**/*.jsonl`）をリポジトリ内にコピー・コミットしてはならない**
- テストフィクスチャは `tests/fixtures/` の合成データのみ使用する（実ログのコピー禁止）
- 社内固有値（Jiraキー、内部リポジトリ名、Slack ID等）をハードコードしない。`config.toml`（gitignore済）経由で渡す

**本リポジトリは現状 PUBLIC である**（Private ではない）。「絶対ルール」は運用上の推奨ではなく、公開リポジトリで実データ漏洩を防ぐための必須要件として扱うこと。将来的に org 移管する余地はあるが、それによって公開範囲が変わるわけではない。

## 技術スタック（決定済み）

- Python 3.12+ / uv / typer / sqlite3（標準ライブラリ）
- GitHub連携は `gh` CLI の subprocess 呼び出し（PyGithub不使用。認証を `gh auth` に委譲するため）
- DB配置: デフォルト `~/.claude/plugins/data/effort-db/effort.db`、環境変数 `EFFORT_DB_PATH` で上書き可
- CLIコマンド名: `effort-db`

## ディレクトリ構成（予定）

```
agent-effort-db/
├── pyproject.toml          # [project.scripts] effort-db = "effort_db.cli:app"
├── src/effort_db/
│   ├── cli.py              # typer アプリ
│   ├── schema.py           # DDL + マイグレーション
│   ├── config.py           # DBパス等の設定解決
│   ├── collectors/
│   │   ├── session.py      # jsonl パーサ（最重要・作り込みポイント）
│   │   └── github.py       # gh CLI ラッパー
│   └── linker.py           # issue_key 抽出・突き合わせ
└── tests/
    └── fixtures/           # 合成jsonlのみ
```

## データモデル（raw層 + 集約ビューの2層構成）

`sessions`（セッション単位の実測値）と `pull_requests`（PR単位のメタ情報）を raw層として持ち、`issue_key` で join した集約ビュー（例: `task_effort`）を参照側に提供する。

- 生データを潰して集約値だけ保存しない。raw層は必ず残す
- インクリメンタル収集が冪等になるよう `session_id` / `(repo, pr_number)` をユニークキーにして upsert する
- `issue_key` が抽出できないレコードも `unlinked` として捨てずに保持する

## CLI コマンド体系（予定）

```
effort-db init                      # DB作成
effort-db backfill sessions         # 過去jsonl走査
effort-db backfill prs --repo xxx   # gh経由でPR取得
effort-db collect-session <id>      # インクリメンタル用（将来hookから）
effort-db stats                     # join率などの健全性確認
effort-db query "..."               # 素のSQL逃げ道
```

## 実装時の注意点

- `collectors/session.py` で turns / tool_calls / wall_clock をどのフィールドから数えるかは、実ログ構造を見ながら探索的に決める（ログバージョン差異の可能性がある）。探索で見た実ログ断片をそのままテストに固定しないこと
- `linker.py`（ブランチ名・コミットメッセージからのチケットキー抽出）実装後は `effort-db stats` で join率を確認し、低ければブランチ命名規約の見直しを先に行う

## 未決定事項（後回し）

- インクリメンタル収集の方式（Stop hook / SessionEnd hook / PR作成コマンド後段 / cron）
- skill / MCP / プラグインとしての参照側の実装
- チーム共有DB化（SQLite → Cloud SQL / BigQuery 等）
- Codex等の他エージェントセッションの取り込み（リポジトリ名を `agent-` としたのはこの余地のため）
- org への移管、公開範囲の拡大

## AI-SDD Instructions (v4.0.1)

<!-- sdd-workflow version: "4.0.1" -->

This project follows AI-SDD (AI-driven Specification-Driven Development) workflow.

### Document Operations

When operating files under `.sdd/` directory, refer to `.sdd/AI-SDD-PRINCIPLES.md` to ensure proper AI-SDD workflow compliance.

**Trigger Conditions**:

- Reading or modifying files under `.sdd/`
- Creating new specifications, design docs, or requirement docs
- Implementing features that reference `.sdd/` documents

For detailed directory structure, file naming convention, and document link convention, refer to `.claude/rules/ai-sdd-instructions.md`.
