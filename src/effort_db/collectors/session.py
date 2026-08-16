"""Claude Code セッションログ（`~/.claude/projects/**/*.jsonl`）のパーサ。

1 ファイル = 1 セッションとして `sessions` の 1 行を導出する。

ログ形式にはバージョン差異があるため、どの列も「新しい形式にしか無いフィールド」に
依存させない方針を採る。列ごとの導出根拠は各ヘルパーのコメントに残す。
取れない列は NULL にし、行自体は必ず残す（件数を減らさない）。

**メッセージ本文は一切保存しない。** 本文は turns / tool_calls / interrupted を
数えるためだけに読み、DB には件数・時刻・ID のみを書く。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# サブエージェント transcript が置かれるディレクトリ名。_add_subagent_tool_calls 参照。
_SUBAGENT_DIR_NAME = "subagents"

# セッション本体のファイル名は UUID。詳細は iter_session_files を参照。
_SESSION_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

# 中断時にログへ残るテキスト。文言のバリエーションを拾うため前方一致で見る。
_INTERRUPT_TEXT_PREFIX = "[Request interrupted"

# ユーザ名にドットが含まれ得る（例: /Users/first.last）ため、ホスト名候補から外す
# ディレクトリ。詳細は _repo_from_path を参照。
_HOME_ROOT_NAMES = frozenset({"Users", "home"})


@dataclass(frozen=True)
class SessionRecord:
    """`sessions` の 1 行。取れなかった列は None。"""

    session_id: str
    repo: str | None = None
    branch: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    wall_clock_min: float | None = None
    turns: int | None = None
    tool_calls: int | None = None
    interrupted: bool = False


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
    """1 ファイルから 1 行分の SessionRecord を作る。壊れた行は読み飛ばす。"""
    record = _build_record(path.stem, path.parent.name, _iter_entries(path))
    return _add_subagent_tool_calls(record, path)


def upsert_session(conn: sqlite3.Connection, record: SessionRecord) -> None:
    """session_id を一意キーに upsert する（commit は呼び出し側）。"""
    params: dict[str, Any] = asdict(record)
    params["interrupted"] = int(record.interrupted)
    conn.execute(_UPSERT_SQL, params)


def collect_all(conn: sqlite3.Connection, projects_dir: Path | None = None) -> int:
    """全 jsonl を走査して upsert し、処理したセッション数を返す。"""
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
# 再取り込みで更新する。
_UPSERT_SQL = """
INSERT INTO sessions (session_id, repo, branch, started_at, ended_at,
                      wall_clock_min, turns, tool_calls, interrupted)
VALUES (:session_id, :repo, :branch, :started_at, :ended_at,
        :wall_clock_min, :turns, :tool_calls, :interrupted)
ON CONFLICT(session_id) DO UPDATE SET
  repo           = excluded.repo,
  branch         = excluded.branch,
  started_at     = excluded.started_at,
  ended_at       = excluded.ended_at,
  wall_clock_min = excluded.wall_clock_min,
  turns          = excluded.turns,
  tool_calls     = excluded.tool_calls,
  interrupted    = excluded.interrupted
"""


def _iter_entries(path: Path) -> Iterator[dict[str, Any]]:
    """jsonl を 1 行 1 エントリとして読む。空行・壊れた行・非オブジェクトは無視。"""
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                # 書き込み途中で切れた行が末尾に残ることがある。1 行の破損で
                # セッション全体を落とさない。
                continue
            if isinstance(entry, dict):
                yield entry


def _build_record(
    session_id: str, project_dir_name: str, entries: Iterator[dict[str, Any]]
) -> SessionRecord:
    timestamps: list[datetime] = []
    branches: Counter[str] = Counter()
    cwds: list[str] = []
    turns = 0
    tool_calls = 0
    saw_message = False
    interrupted = False

    for entry in entries:
        timestamp = _parse_timestamp(entry.get("timestamp"))
        if timestamp is not None:
            timestamps.append(timestamp)

        branch = entry.get("gitBranch")
        if isinstance(branch, str) and branch:
            branches[branch] += 1

        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd:
            cwds.append(cwd)

        # 中断シグナルは 2 系統ある。interruptedMessageId は新しい形式の構造
        # フィールドで確実だが、実ログではテキストマーカーだけを持つセッションも
        # 存在した（マーカー側が上位集合）。取りこぼしを避けるため両方を見る。
        if entry.get("interruptedMessageId") is not None:
            interrupted = True

        entry_type = entry.get("type")
        content, blocks = _message_content(entry)

        if entry_type in ("user", "assistant"):
            saw_message = True
        if _has_interrupt_marker(content, blocks):
            interrupted = True

        # tool_calls は assistant が発行した tool_use ブロック数で数える。
        # tool_result 側で数えるとツール失敗時のリトライ等で件数がずれる。
        # 同一ファイル内のサブエージェント（isSidechain）の呼び出しも実作業なので含める。
        tool_calls += _count_tool_uses(blocks)

        if entry_type == "user" and _is_human_prompt(entry, content, blocks):
            turns += 1

    started_at = min(timestamps) if timestamps else None
    ended_at = max(timestamps) if timestamps else None
    wall_clock_min = (
        round((ended_at - started_at).total_seconds() / 60, 2)
        if started_at is not None and ended_at is not None
        else None
    )

    return SessionRecord(
        session_id=session_id,
        repo=_resolve_repo(cwds, project_dir_name),
        branch=_dominant_branch(branches),
        started_at=started_at.isoformat() if started_at is not None else None,
        ended_at=ended_at.isoformat() if ended_at is not None else None,
        wall_clock_min=wall_clock_min,
        # メッセージが 1 件も無いログ（空ファイル / メタ行のみ）ではターン数も
        # ツール呼び出し数も「0 件」と断定できないので NULL にする。
        turns=turns if saw_message else None,
        tool_calls=tool_calls if saw_message else None,
        interrupted=interrupted,
    )


def _add_subagent_tool_calls(record: SessionRecord, path: Path) -> SessionRecord:
    """`<session-uuid>/subagents/*.jsonl` の tool_use を tool_calls に加算する。

    サブエージェントの作業は別ファイルに分離して記録されるが、これも同じセッションで
    エージェントが実際に行った作業なので tool_calls に含める（ディレクトリ名が親の
    session UUID なので帰属は一意）。含めないと、サブエージェントを多用する
    セッションのツール呼び出し数が大きく過小評価される。

    turns は加算しない。サブエージェントには人間のプロンプトが無いため。
    """
    subagent_dir = path.parent / path.stem / _SUBAGENT_DIR_NAME
    if not subagent_dir.is_dir():
        return record

    extra = sum(
        _count_tool_uses(blocks)
        for file in sorted(subagent_dir.glob("*.jsonl"))
        for _, blocks in map(_message_content, _iter_entries(file))
    )
    if extra == 0:
        return record
    return replace(record, tool_calls=(record.tool_calls or 0) + extra)


def _message_content(entry: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    """エントリの message.content と、その中の dict ブロックだけを返す。"""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return content, []
    return content, [block for block in content if isinstance(block, dict)]


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
