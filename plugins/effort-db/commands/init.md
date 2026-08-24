---
description: effort-db の DB を作成する（冪等）
allowed-tools: Bash(uv:*)
---

`effort-db init` を実行し、DB とテーブル/ビューを作成する（既に存在する場合は何もしない）。

1. `uv --version` を確認する。無ければ https://docs.astral.sh/uv/ の案内を提示して終了する。
2. `uv run --project "${CLAUDE_PLUGIN_ROOT}/../.." effort-db init` を実行する。
   - `uv run` は未セットアップの venv でも自動的に依存関係を sync してから実行するため、
     初回のみ数十秒かかることがある。
3. 標準出力・標準エラーをそのまま提示する。
