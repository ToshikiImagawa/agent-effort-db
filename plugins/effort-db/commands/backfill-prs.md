---
description: gh経由でマージ済みPRを取り込む
argument-hint: --repo owner/repo [--limit 50]
allowed-tools: Bash(uv:*)
---

`effort-db backfill prs` を実行し、`gh` CLI 経由でマージ済み PR を取り込む（冪等）。

1. `uv --version` を確認する。無ければ https://docs.astral.sh/uv/ の案内を提示して終了する。
2. `uv run --project "${CLAUDE_PLUGIN_ROOT}/../.." effort-db backfill prs $ARGUMENTS` を実行する。
   - `$ARGUMENTS` には `--repo owner/repo` を必ず含める（`--limit` は任意、デフォルト 50）。
   - `uv run` は未セットアップの venv でも自動的に依存関係を sync してから実行するため、
     初回のみ数十秒かかることがある。
3. `gh` が未認証・未インストールの場合はエラーが stderr に出るので、そのまま提示する
   （`gh auth login` 等の対応をユーザーに委ねる）。
