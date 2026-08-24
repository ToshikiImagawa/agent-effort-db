---
description: 収集状況・join率などの健全性を表示する（読み取り専用）
allowed-tools: Bash(uv:*)
---

`effort-db stats` を実行し、由来ごとの join 率や issue_key 付与率などの健全性指標を表示する
（読み取り専用）。

1. `uv --version` を確認する。無ければ https://docs.astral.sh/uv/ の案内を提示して終了する。
2. `uv run --project "${CLAUDE_PLUGIN_ROOT}/../.." effort-db stats` を実行する。
   - `uv run` は未セットアップの venv でも自動的に依存関係を sync してから実行するため、
     初回のみ数十秒かかることがある。
3. 出力をそのまま提示する。join 率が低い等の警告が出た場合はその内容も伝える。
