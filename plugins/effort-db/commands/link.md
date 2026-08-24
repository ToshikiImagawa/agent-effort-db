---
description: セッションとPRを突き合わせる
allowed-tools: Bash(uv:*)
---

`effort-db link` を実行し、セッションと PR の突き合わせを段階適用する（冪等）。
`log_reference` → `repo_branch` の順で確実性の高いキーから適用され、`issue_key` の付与も行う。

1. `uv --version` を確認する。無ければ https://docs.astral.sh/uv/ の案内を提示して終了する。
2. `uv run --project "${CLAUDE_PLUGIN_ROOT}/../.." effort-db link` を実行する。
   - `uv run` は未セットアップの venv でも自動的に依存関係を sync してから実行するため、
     初回のみ数十秒かかることがある。
3. 標準出力（由来ごとの件数、`issue_key` 付与数、`unlinked` 件数）をそのまま提示する。
