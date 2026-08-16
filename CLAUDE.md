# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

Claude Code / AI エージェントの実工数を収集・蓄積するローカルDB & CLI（sessions × PRs × Jira）。

Claude Code が出す工数見積もりは「人間が実施する前提」で大きく外れることが多い。そこで**実際にかかった工数を実測データから蓄積し、見積もりの指標（reference class）として使えるDB** を作ることが目的。

**CLI コマンド一式は実装済み**（`init` / `backfill sessions` / `backfill prs` / `collect-session` / `stats` / `relink` / `query`）。
ただし**実測データに基づく設計判断に実装が追随していない項目が 5 つ残る**（最重要: join キー。現状の実装では実データに 1 件も紐付かない）。
差分の一覧は `_design.md` の 1.1、残作業は [ROADMAP.md](ROADMAP.md) の M7 を参照すること。

実装状況は記述より実態が真実。着手前に必ず Glob/Grep で検証する。

## 参照すべきドキュメント

以下が本プロジェクトの真実の源である。実装・設計判断の前に参照すること。

| ドキュメント                                          | 役割                                                     |
|:------------------------------------------------|:-------------------------------------------------------|
| [CONSTITUTION.md](.sdd/CONSTITUTION.md)         | 交渉不可の原則（B/A/D/T）。設計・実装はすべてこれに準拠する                      |
| [effort-db.md](.sdd/requirement/effort-db.md)   | PRD。何を・なぜ作るか（要求 ID: REQ / FR / PR / IR / DC）            |
| [ROADMAP.md](ROADMAP.md)                        | M0〜M7 のマイルストーンと完了条件。実装順序と現在地                           |

以降のセクションは決定済みの設計方針の要約である。詳細と根拠は上記ドキュメントが持つ。
記述が食い違う場合は CONSTITUTION.md と PRD を優先する。

## 設計判断の背景

- GitHub の issue/PR の経過時間はレビュー待ち・放置を含みノイジー → 補助的な特徴量として使う
- **本命は Claude Code 自身のセッションログ**（`~/.claude/projects/**/*.jsonl`）。ターン数・ツール呼び出し回数・実経過時間が取れる
- 見積もり単位は「人日」ではなく「セッション数 / ターン数 / 実時間」などエージェントネイティブな単位とし、点推定ではなく分布（中央値/p90）で扱う
- セッション ↔ PR の join キーは **`(repo, branch)` を主軸**とし、セッションログ内の PR 参照が得られる場合はそれを優先する。チケットキーは補助的な集約キーであり、単独の主キーとしない（形式は `config.toml` から与える。ドキュメント・コード中に実在のプロジェクトキーを書かない）
    - 実ログ調査の結果、ブランチ名にチケットキーを含むものは 1.3% にとどまり、チケットキー主軸では集約が成立しないことが判明した。観測結果は `_design.md` の 9.1、判断の根拠は 9.2（D6）にある
- 紐付かないレコードは捨てず `unlinked` として保持する。リンクには**由来**（どのキーで紐付いたか）を記録し、join 率をキー種別ごとに観測できるようにする

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
│   ├── cli.py              # typer アプリ（全コマンド実装済み）
│   ├── schema.py           # DDL + マイグレーション（v1 のみ。v2 は M7-C）
│   ├── config.py           # DBパス / issue_key パターンの解決（実装済み）
│   └── collectors/
│       ├── session.py      # jsonl パーサ（実装済み・最重要の作り込みポイント）
│       └── github.py       # gh CLI ラッパー（実装済み）
│   └── linker.py           # issue_key 抽出 / relink / 健全性集計（実装済み）
└── tests/
    ├── fixtures/projects/  # 合成jsonlのみ（実ログのコピー禁止）
    └── test_*.py           # 61 件
```

`models.py` / `stats.py` は `_design.md` の設計上のモジュール分割であり、現在は未作成
（型は各モジュール内に、健全性集計は `linker.py` に置かれている）。

CLI は `init` / `backfill sessions` / `backfill prs` / `collect-session` / `link` / `stats` / `query`。
`link` を独立コマンドとするのは、PR を後から取り込んだ際にセッション再収集なしでリンクを作り直せるようにするため。

## データモデル（raw層 + リンク層 + 集約ビューの3層構成）

- **raw層**: `sessions`（セッション単位の実測値）/ `pull_requests`（PR単位のメタ情報）/ `session_pr_refs`（ログから抽出した PR 参照）
- **リンク層**: `session_pr_links`（セッションと PR の対応を**由来付き**で保持）
- **集約層**: `effort_by_branch`（`(repo, branch)` 単位）/ `effort_by_issue`（チケットキーが付与された分のみ）

詳細な DDL は `_design.md` の 5 章にある。

- 生データを潰して集約値だけ保存しない。raw層は必ず残す
- インクリメンタル収集が冪等になるよう `session_id` / `(repo, pr_number)` をユニークキーにして upsert する
- 突き合わせキーが得られないレコードも `unlinked` として捨てずに保持する
- 集約ビューでリンクを JOIN すると 1 セッションが複数 PR に紐づく場合に重複計上される。PR 側の値は相関サブクエリで求める

## CLI コマンド体系（予定）

```
effort-db init                      # DB作成（実装済み）
effort-db backfill sessions         # 過去jsonl走査
effort-db backfill prs --repo xxx   # gh経由でPR取得
effort-db collect-session <id>      # インクリメンタル用（将来hookから）
effort-db link                      # 突き合わせを段階適用（収集とは独立）
effort-db stats                     # キー種別ごとのjoin率など健全性確認
effort-db query "..."               # 素のSQL逃げ道
```

## 実装時の注意点

- **実ログ構造の調査は完了している**。観測結果は `_design.md` 9.1（O1〜O17）、そこから導いた判断は 9.2（D1〜D18）にある。実装前に必ず参照すること。推測で作り直さない
- **観測を追記する際は「何を母集団としたか」を必ず書く**（9.1 の「標本」列）。セッション本体は `<project>/<UUID>.jsonl` の 1 階層のみで、`subagents/` と `workflows/` は本体ではない。これを混ぜて集計した結果、存在しない問題に対して設計してしまった前例がある（`_design.md` v0.3 の教訓）
- 特に注意すべき観測事実:
    - ターン数は**人由来の発話のみ**を数える。`user` レコードの約 91% はツール応答であり、含めると意味を失う（D1）
    - ツール呼び出しの約 37% がサブエージェント内。主エージェントと別列で保持する（D2）
    - 実経過時間は min / max で求める。時刻は約 64% のファイルで逆行する（D5 / O17）
    - ブランチ名にチケットキーを含むものは実データに 1 件も無い。`issue_key` 単独では join できない（D6 / O10）
    - セッション本体は 1 ファイル = 1 セッションで、ファイル名が識別子と 100% 一致する（D3 / D4 / O4 / O8）
- 実装中に新しい観測事実が見つかったら、**まず `_design.md` 9.1 に追記し、その後 9.2 の判断を更新する**。判断を先に書いて後から根拠を探さない
- 探索で見た実ログ断片をそのままテストに固定しないこと。テストは「値」ではなく「関係」を検証する（例: ツール応答を N 件加えてもターン数が変わらない）
- `stats` は join 率を**キー種別ごとに**表示する。低い場合は抽出ロジックの複雑化ではなく、より確実なキーの採用または命名規約の見直しを先に検討する

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
