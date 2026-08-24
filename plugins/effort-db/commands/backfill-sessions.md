---
description: 過去セッションログを一括取り込みする
allowed-tools: Bash(uv:*)
---

`effort-db backfill sessions` を実行し、`~/.claude/projects/**/*.jsonl`
（`CLAUDE_CONFIG_DIR` が設定されていればそちらの `projects/`）を全走査して取り込む（冪等）。

1. `uv --version` を確認する。無ければ https://docs.astral.sh/uv/ の案内を提示して終了する。
2. `uv run --project "${CLAUDE_PLUGIN_ROOT}/../.." effort-db backfill sessions` を実行する。
   - `uv run` は未セットアップの venv でも自動的に依存関係を sync してから実行するため、
     初回のみ数十秒かかることがある。
3. 標準出力・標準エラーをそのまま提示する。
