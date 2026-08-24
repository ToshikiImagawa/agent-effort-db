---
description: 任意SQLで参照する（読み取り専用、PRAGMA query_only）
argument-hint: <SQL>
allowed-tools: Bash(uv:*)
---

`effort-db query` を実行し、任意の SQL を読み取り専用（`PRAGMA query_only`）で実行する。

1. `uv --version` を確認する。無ければ https://docs.astral.sh/uv/ の案内を提示して終了する。
2. `uv run --project "${CLAUDE_PLUGIN_ROOT}/../.." effort-db query "$ARGUMENTS"` を実行する
   （`$ARGUMENTS` は SELECT 文。書き込み文は `PRAGMA query_only` により拒否される）。
   - `uv run` は未セットアップの venv でも自動的に依存関係を sync してから実行するため、
     初回のみ数十秒かかることがある。
3. タブ区切りの結果（1行目はヘッダ、`NULL` は文字通り `NULL` と表示される）をそのまま提示する。
