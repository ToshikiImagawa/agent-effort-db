"""Claude Code セッションログ（`~/.claude/projects/**/*.jsonl`）のパーサ。

1 ファイル = 1 セッションとして `sessions` の 1 行を導出し、ログに含まれていた
PR 参照を `session_pr_refs` の行として保存する。

ログ形式にはバージョン差異があるため、どの列も「新しい形式にしか無いフィールド」に
依存させない方針を採る。列ごとの導出根拠は各ヘルパーのコメントに残す。
取れない列は NULL にし、行自体は必ず残す（件数を減らさない）。

**主エージェントとサブエージェントの作業量は別々に数える。** 合算すると主エージェント
分を後から復元できない（design D2 / D16 / D19）。ツール呼び出しもトークンも同様。

**メッセージ本文は一切保存しない。** 本文は turns / tool_calls / interrupted を
数えるためだけに読み、DB には件数・時刻・ID のみを書く。

ファイルは行単位でストリーミングして読む。最大級のログ（数十 MB）でも定常メモリで
完走させるため、全行をリストに載せない（NFR-008）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# サブエージェント transcript が置かれるディレクトリ名。_consume_subagent_logs 参照。
_SUBAGENT_DIR_NAME = "subagents"

# セッション本体のファイル名は UUID。詳細は iter_session_files を参照。
_SESSION_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

# 中断時にログへ残るテキスト。文言のバリエーションを拾うため前方一致で見る。
_INTERRUPT_TEXT_PREFIX = "[Request interrupted"

# PR 参照専用のレコード種別。prNumber（整数）と prRepository（owner/repo）を持つ。
_PR_LINK_TYPE = "pr-link"

# usage のキーと、それを入れる列名の対応。ログ側のキー名変更をここだけに閉じ込める。
_TOKEN_KEYS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
)

# ユーザ名にドットが含まれ得る（例: /Users/first.last）ため、ホスト名候補から外す
# ディレクトリ。詳細は _repo_from_path を参照。
_HOME_ROOT_NAMES = frozenset({"Users", "home"})


@dataclass(frozen=True)
class SessionRecord:
    """`sessions` の 1 行と、そのログから観測した PR 参照。取れなかった列は None。

    `pr_refs` は `sessions` の列ではなく `session_pr_refs` の行になる。
    ログに書かれていた事実そのものであり、そこから導いたリンク（`session_pr_links`）
    とは別の層に保存する。
    """

    session_id: str
    repo: str | None = None
    branch: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    wall_clock_min: float | None = None
    turns: int | None = None
    tool_calls: int | None = None
    sidechain_tool_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    sidechain_input_tokens: int | None = None
    sidechain_output_tokens: int | None = None
    sidechain_cache_read_tokens: int | None = None
    sidechain_cache_creation_tokens: int | None = None
    interrupted: bool = False
    log_versions: str | None = None
    skipped_records: int = 0
    pr_refs: tuple[tuple[str, int], ...] = ()


def iter_session_files(projects_dir: Path | None = None) -> Iterator[Path]:
    """セッション本体の jsonl を列挙する。ディレクトリが無ければ何も返さない。

    セッション本体は `<project>/<UUID>.jsonl` の 1 階層だけに置かれる。同じツリー
    には以下のセッションでない transcript も居るため、深さとファイル名の両方で絞る。

    - `<project>/<session-uuid>/subagents/agent-*.jsonl`（サブエージェント）
    - `<project>/<session-uuid>/workflows/.../journal.jsonl`（workflow ジャーナル）

    特に journal.jsonl は複数セッションで同名なので、拾うと session_id が衝突する。
    """
    base = projects_dir if projects_dir is not None else DEFAULT_PROJECTS_DIR
    if not base.is_dir():
        return
    yield from sorted(
        path for path in base.glob("*/*.jsonl") if _SESSION_ID_PATTERN.fullmatch(path.stem)
    )


def parse_session_file(path: Path) -> SessionRecord:
    """1 ファイル（+ そのサブエージェント transcript）から 1 行分を作る。

    段を分けてある: 走査（_consume_*）で数え、組み立て（_build_record）で列を決める。
    実測値の定義を組み立て側に集約し、ログ形式への依存を走査側に閉じ込めるため。
    """
    tally = _Tally()
    _consume_main_log(tally, path)
    _consume_subagent_logs(tally, path)
    return _build_record(path.stem, path.parent.name, tally)


def upsert_session(conn: sqlite3.Connection, record: SessionRecord) -> None:
    """session_id を一意キーに upsert し、PR 参照を保存する（commit は呼び出し側）。"""
    params: dict[str, Any] = asdict(record)
    params["interrupted"] = int(record.interrupted)
    # pr_refs は sessions の列ではなく別テーブルの行なので、名前付きパラメータから外す。
    params.pop("pr_refs")
    conn.execute(_UPSERT_SQL, params)
    conn.executemany(
        # 再収集で同じ参照が来ても増えないように OR IGNORE。ログから消えることは
        # 無い（追記のみ）ので、既存行の削除は行わない。
        "INSERT OR IGNORE INTO session_pr_refs (session_id, repo, pr_number) VALUES (?, ?, ?)",
        [(record.session_id, repo, pr_number) for repo, pr_number in record.pr_refs],
    )


def collect_all(conn: sqlite3.Connection, projects_dir: Path | None = None) -> int:
    """全 jsonl を走査して upsert し、処理したセッション数を返す。

    常に全走査する。増分判定を持たないのは、全走査が実データで 8.4 秒で完走し
    （design O25）、判定を入れても得られる短縮が数秒である一方、取りこぼしの経路と
    それを回避するオプションを恒久的に抱えることになるため（D13）。
    """
    count = 0
    for path in iter_session_files(projects_dir):
        upsert_session(conn, parse_session_file(path))
        count += 1
    conn.commit()
    return count


def collect_one(
    conn: sqlite3.Connection, session_id: str, projects_dir: Path | None = None
) -> bool:
    """単一セッションを upsert する。該当ログが無ければ False。"""
    for path in iter_session_files(projects_dir):
        if path.stem != session_id:
            continue
        upsert_session(conn, parse_session_file(path))
        conn.commit()
        return True
    return False


# issue_key を列に含めないのは意図的。linker が後から埋める値を再取り込みで
# 消さないため。進行中セッションは ended_at / turns が増えるので、それ以外の列は
# 再取り込みで更新する。collected_at は「いつ時点のデータか」を示す観測値。
_UPSERT_SQL = """
INSERT INTO sessions (
  session_id, repo, branch, started_at, ended_at, wall_clock_min,
  turns, tool_calls, sidechain_tool_calls, interrupted,
  input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
  sidechain_input_tokens, sidechain_output_tokens,
  sidechain_cache_read_tokens, sidechain_cache_creation_tokens,
  log_versions, skipped_records, collected_at
)
VALUES (
  :session_id, :repo, :branch, :started_at, :ended_at, :wall_clock_min,
  :turns, :tool_calls, :sidechain_tool_calls, :interrupted,
  :input_tokens, :output_tokens, :cache_read_tokens, :cache_creation_tokens,
  :sidechain_input_tokens, :sidechain_output_tokens,
  :sidechain_cache_read_tokens, :sidechain_cache_creation_tokens,
  :log_versions, :skipped_records, datetime('now')
)
ON CONFLICT(session_id) DO UPDATE SET
  repo                            = excluded.repo,
  branch                          = excluded.branch,
  started_at                      = excluded.started_at,
  ended_at                        = excluded.ended_at,
  wall_clock_min                  = excluded.wall_clock_min,
  turns                           = excluded.turns,
  tool_calls                      = excluded.tool_calls,
  sidechain_tool_calls            = excluded.sidechain_tool_calls,
  interrupted                     = excluded.interrupted,
  input_tokens                    = excluded.input_tokens,
  output_tokens                   = excluded.output_tokens,
  cache_read_tokens               = excluded.cache_read_tokens,
  cache_creation_tokens           = excluded.cache_creation_tokens,
  sidechain_input_tokens          = excluded.sidechain_input_tokens,
  sidechain_output_tokens         = excluded.sidechain_output_tokens,
  sidechain_cache_read_tokens     = excluded.sidechain_cache_read_tokens,
  sidechain_cache_creation_tokens = excluded.sidechain_cache_creation_tokens,
  log_versions                    = excluded.log_versions,
  skipped_records                 = excluded.skipped_records,
  collected_at                    = datetime('now')
"""


@dataclass
class _Tokens:
    """トークン使用量の内訳。主エージェント分とサブエージェント分で 1 組ずつ使う。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, usage: Any) -> None:
        """assistant レコードの usage を加算する。数値でない値は 0 として扱う。"""
        if not isinstance(usage, dict):
            return
        for log_key, column in _TOKEN_KEYS:
            value = usage.get(log_key)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self, column, getattr(self, column) + value)


@dataclass
class _Tally:
    """走査中の集計値。可変オブジェクトにするのは、行単位で読みながら数えるため。"""

    timestamps: list[datetime] = field(default_factory=list)
    branches: Counter[str] = field(default_factory=Counter)
    cwds: list[str] = field(default_factory=list)
    # dict をそのまま順序付き集合として使う（同一 PR への参照は複数回現れる）。
    pr_refs: dict[tuple[str, int], None] = field(default_factory=dict)
    versions: set[str] = field(default_factory=set)
    turns: int = 0
    tool_calls: int = 0
    sidechain_tool_calls: int = 0
    tokens: _Tokens = field(default_factory=_Tokens)
    sidechain_tokens: _Tokens = field(default_factory=_Tokens)
    saw_message: bool = False
    saw_subagent_log: bool = False
    interrupted: bool = False
    skipped_records: int = 0


def _consume_main_log(tally: _Tally, path: Path) -> None:
    """セッション本体のログを 1 行ずつ数える。"""
    for entry in _iter_entries(path, tally):
        timestamp = _parse_timestamp(entry.get("timestamp"))
        if timestamp is not None:
            tally.timestamps.append(timestamp)

        branch = entry.get("gitBranch")
        if isinstance(branch, str) and branch:
            tally.branches[branch] += 1

        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd:
            tally.cwds.append(cwd)

        version = entry.get("version")
        if isinstance(version, str) and version:
            tally.versions.add(version)

        # 中断シグナルは 2 系統ある。interruptedMessageId は新しい形式の構造
        # フィールドで確実だが、実ログではテキストマーカーだけを持つセッションも
        # 存在した（マーカー側が上位集合）。取りこぼしを避けるため両方を見る。
        if entry.get("interruptedMessageId") is not None:
            tally.interrupted = True

        entry_type = entry.get("type")

        if entry_type == _PR_LINK_TYPE:
            ref = _pr_ref(entry)
            if ref is not None:
                tally.pr_refs[ref] = None
            continue

        content, blocks = _message_content(entry)

        if entry_type in ("user", "assistant"):
            tally.saw_message = True
        if _has_interrupt_marker(content, blocks):
            tally.interrupted = True

        # tool_calls は assistant が発行した tool_use ブロック数で数える。
        # tool_result 側で数えるとツール失敗時のリトライ等で件数がずれる。
        # 現行形式ではサブエージェントの会話は本体ファイルに現れない（O19）が、
        # 旧形式で混在していた場合に主エージェント分を過大にしないよう振り分ける。
        if entry.get("isSidechain"):
            tally.sidechain_tool_calls += _count_tool_uses(blocks)
            tally.sidechain_tokens.add(_usage(entry))
        else:
            tally.tool_calls += _count_tool_uses(blocks)
            tally.tokens.add(_usage(entry))

        if entry_type == "user" and _is_human_prompt(entry, content, blocks):
            tally.turns += 1


def _consume_subagent_logs(tally: _Tally, path: Path) -> None:
    """`<session-uuid>/subagents/*.jsonl` をサブエージェント分として数える。

    サブエージェントの作業は別ファイルに記録されるが、同じセッションでエージェントが
    実際に行った作業なので親セッションに帰属させる（ディレクトリ名が親の session
    UUID なので帰属は一意）。無視すると、サブエージェントを多用するセッションの
    ツール呼び出し数が大きく過小評価される（全体の約 37%）。

    ただし主エージェント分とは別列に入れる（D2 / D16 / D19）。turns には加算しない
    （サブエージェントには人間のプロンプトが無い）。時刻・ブランチ・cwd も取らない
    （セッションの実経過時間と帰属先はあくまで本体の観測から決める）。
    """
    subagent_dir = path.parent / path.stem / _SUBAGENT_DIR_NAME
    if not subagent_dir.is_dir():
        return

    tally.saw_subagent_log = True
    for file in sorted(subagent_dir.glob("*.jsonl")):
        for entry in _iter_entries(file, tally):
            version = entry.get("version")
            if isinstance(version, str) and version:
                tally.versions.add(version)
            _, blocks = _message_content(entry)
            tally.sidechain_tool_calls += _count_tool_uses(blocks)
            tally.sidechain_tokens.add(_usage(entry))


def _build_record(session_id: str, project_dir_name: str, tally: _Tally) -> SessionRecord:
    """集計値から `sessions` の 1 行を組み立てる。実測値の定義はここに集約する。"""
    started_at = min(tally.timestamps) if tally.timestamps else None
    ended_at = max(tally.timestamps) if tally.timestamps else None
    wall_clock_min = (
        round((ended_at - started_at).total_seconds() / 60, 2)
        if started_at is not None and ended_at is not None
        else None
    )
    # メッセージが 1 件も無いログ（空ファイル / メタ行のみ）では、件数もトークンも
    # 「0 件」と断定できないので NULL にする。0（観測できて 0）と区別する。
    observed = tally.saw_message
    # サブエージェントは、記録が無ければ「使わなかった」と言える（ディレクトリが
    # 無い = 起動していない）。ただし本体が観測できていない場合は NULL に倒す。
    sidechain_observed = observed or tally.saw_subagent_log

    return SessionRecord(
        session_id=session_id,
        repo=_resolve_repo(tally.cwds, project_dir_name),
        branch=_dominant_branch(tally.branches),
        started_at=started_at.isoformat() if started_at is not None else None,
        ended_at=ended_at.isoformat() if ended_at is not None else None,
        wall_clock_min=wall_clock_min,
        turns=tally.turns if observed else None,
        tool_calls=tally.tool_calls if observed else None,
        sidechain_tool_calls=tally.sidechain_tool_calls if sidechain_observed else None,
        input_tokens=tally.tokens.input_tokens if observed else None,
        output_tokens=tally.tokens.output_tokens if observed else None,
        cache_read_tokens=tally.tokens.cache_read_tokens if observed else None,
        cache_creation_tokens=tally.tokens.cache_creation_tokens if observed else None,
        sidechain_input_tokens=(
            tally.sidechain_tokens.input_tokens if sidechain_observed else None
        ),
        sidechain_output_tokens=(
            tally.sidechain_tokens.output_tokens if sidechain_observed else None
        ),
        sidechain_cache_read_tokens=(
            tally.sidechain_tokens.cache_read_tokens if sidechain_observed else None
        ),
        sidechain_cache_creation_tokens=(
            tally.sidechain_tokens.cache_creation_tokens if sidechain_observed else None
        ),
        interrupted=tally.interrupted,
        # 観測したバージョンを並べて保持する。形式差異で欠損が出た場合に、どの
        # バージョンで起きたかを後から SQL で追える（NFR-004）。
        log_versions=",".join(sorted(tally.versions)) or None,
        skipped_records=tally.skipped_records,
        pr_refs=tuple(tally.pr_refs),
    )


def _iter_entries(path: Path, tally: _Tally) -> Iterator[dict[str, Any]]:
    """jsonl を 1 行 1 エントリとして読む。空行は無視し、解釈できない行は数える。"""
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                # 書き込み途中で切れた行が末尾に残ることがある。1 行の破損で
                # セッション全体を落とさない（NFR-003）。
                tally.skipped_records += 1
                continue
            if isinstance(entry, dict):
                yield entry
            else:
                # JSON としては妥当だがレコードではない（配列・数値等）。
                tally.skipped_records += 1


def _pr_ref(entry: dict[str, Any]) -> tuple[str, int] | None:
    """PR 参照レコードから `(owner/repo, pr_number)` を取り出す。解釈できなければ None。

    実ログでは `prRepository` は常に `owner/repo` 形式・`prNumber` は常に整数だが、
    それでも検証する。形式が崩れたときに収集全体を止めず、そのレコードだけを
    落として続けるため。`bool` は `int` の派生なので明示的に除外する。
    """
    repo = entry.get("prRepository")
    pr_number = entry.get("prNumber")
    if not isinstance(repo, str) or repo.count("/") != 1 or repo.startswith("/"):
        return None
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        return None
    if repo.endswith("/"):
        return None
    return repo, pr_number


def _message_content(entry: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    """エントリの message.content と、その中の dict ブロックだけを返す。"""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return content, []
    return content, [block for block in content if isinstance(block, dict)]


def _usage(entry: dict[str, Any]) -> Any:
    """assistant レコードの message.usage を返す（無ければ None）。"""
    message = entry.get("message")
    return message.get("usage") if isinstance(message, dict) else None


def _count_tool_uses(blocks: list[dict[str, Any]]) -> int:
    return sum(1 for block in blocks if block.get("type") == "tool_use")


def _is_human_prompt(entry: dict[str, Any], content: Any, blocks: list[dict[str, Any]]) -> bool:
    """人間が投げたプロンプトかどうか。

    turns は「人間が投げたプロンプト数」で数える。Claude Code 自身が出す
    `system` / `turn_duration` エントリの方が権威的に見えるが、実ログでは
    存在しないファイルが一定割合あり（古いバージョンや、完了しなかったターンでは
    出ない）、バージョン間で定義が揺れる。user エントリは全バージョンに存在する
    ため、そこから以下を除いた件数を採る。

    - `isSidechain`: サブエージェントの会話。人間のプロンプトではない
    - `isMeta`: フックやツールが差し込む擬似 user エントリ
    - `tool_result` ブロックを含むもの: ツール応答（agent のターンの続き）
    """
    if entry.get("isSidechain") or entry.get("isMeta"):
        return False
    if isinstance(content, str):
        return True
    if not blocks:
        return False
    return not any(block.get("type") == "tool_result" for block in blocks)


def _has_interrupt_marker(content: Any, blocks: list[dict[str, Any]]) -> bool:
    if isinstance(content, str):
        return content.startswith(_INTERRUPT_TEXT_PREFIX)
    return any(
        isinstance(text := block.get("text"), str) and text.startswith(_INTERRUPT_TEXT_PREFIX)
        for block in blocks
    )


def _dominant_branch(branches: Counter[str]) -> str | None:
    """最頻のブランチ名を返す（同数なら先に現れた方）。

    セッション途中でブランチが切り替わることがある（main で始めて feature へ等）。
    一瞬だけ滞在したブランチにセッション全体の工数を帰属させたくないので最頻値を採る。
    """
    if not branches:
        return None
    return branches.most_common(1)[0][0]


def _resolve_repo(cwds: list[str], project_dir_name: str) -> str | None:
    """`owner/repo` を導出する。

    プロジェクトディレクトリ名は `/` と `.` の両方が `-` に潰れており
    （`.claude` → `-claude`）owner/repo の境界を一意に復元できない。一方 `cwd` は
    実絶対パスなので無損失なため、cwd を第一候補にする。最初の cwd = セッション
    開始ディレクトリで、これがプロジェクトディレクトリ名の由来でもある。
    """
    for cwd in cwds:
        repo = _repo_from_path(cwd)
        if repo is not None:
            return repo
    if cwds:
        # cwd はあるが <host>/<owner>/<repo> レイアウトではない（/private/tmp 等）。
        return None
    # 実質空のログ（cwd を持つエントリが 1 件も無い）。行を残すため、復元できない
    # ディレクトリ名をそのまま入れる。owner/repo 形式かは "/" の有無で判別できる。
    return project_dir_name or None


def _repo_from_path(cwd: str) -> str | None:
    """`.../<host>/<owner>/<repo>/...` レイアウトから `owner/repo` を取り出す。

    右から見てホスト名らしいセグメント（ドットを含む）を探し、その直後の 2 階層を
    owner/repo とする。`github.com` だけでなく `ghe.example.com` のような社内
    ホストも同じ規則で扱える。右から探すのは、リポジトリ配下にドット入りの
    ディレクトリがあってもホスト側を取り違えないため。
    """
    parts = list(PurePosixPath(cwd).parts)
    if parts and parts[0] == "/":
        parts = parts[1:]
    # /Users/<name> と /home/<name> の <name> はドットを含み得るので候補外にする。
    start = 2 if parts[:1] and parts[0] in _HOME_ROOT_NAMES else 0

    for index in range(len(parts) - 3, start - 1, -1):
        segment = parts[index]
        if "." in segment and not segment.startswith("."):
            return f"{parts[index + 1]}/{parts[index + 2]}"
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # naive と aware が混在すると min()/max() が例外になるため UTC に寄せる。
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
