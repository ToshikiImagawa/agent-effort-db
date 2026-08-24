---
name: effort-distribution
description: >-
  「このタスクはどれくらいかかった」「似たIssueの工数感を知りたい」「工数の分布を見せて」のように、
  実測の作業時間・ターン数・トークン数について自然言語で聞かれたとき、effort-db CLI（stats/query）
  から実測データのみを取得し中央値・p90等の分布として提示する。独自の見積もりロジックは持たない。
allowed-tools: Bash(uv:*)
---

# effort-distribution

`effort-db` が蓄積した実測データ（セッション × PR）から、工数の**分布**を提示する。
推定モデルは持たない。取得できた実測値をそのまま集計するだけである。

## 手順

1. `uv run --project "${CLAUDE_PLUGIN_ROOT}/../.." effort-db stats` で健全性を確認する。
   - join 率（`log_reference` / `repo_branch`）や `issue_key` 付与率が低い場合、
     回答に「参照できる母集団が少ない」旨を明記する。
2. `uv run --project "${CLAUDE_PLUGIN_ROOT}/../.." effort-db query "<SQL>"` で実測行を取得する。
   `effort_by_issue` / `effort_by_branch` は 1 行 = 1 issue_key / 1 (repo, branch) の合計値なので、
   分布を取るには複数行（複数の issue / branch）を集める。例:
   ```sql
   SELECT issue_key, total_min, total_turns, sessions FROM effort_by_issue ORDER BY total_min;
   SELECT repo, branch, total_min, total_turns, sessions FROM effort_by_branch WHERE repo = 'owner/repo';
   ```
   ユーザーが特定の issue_key や repo/branch を挙げた場合は `WHERE` で絞り込む。
3. 取得した `total_min` / `total_turns` 等の列を数値でソートし、件数・中央値・p90 を算出して提示する。
   - 単一の代表値（平均のみ等）だけで答えない。
   - 件数（n）が小さい場合は「参考程度」であることを明記する。
   - 「人日」への換算は行わない。セッション数・ターン数・実分（`total_min`）などの
     観測可能な単位のまま提示する。
4. 母集団が無い（0件）、または join 率が低くて信頼できない場合は「データ不足」と回答し、
   経験や一般論で補完しない。
