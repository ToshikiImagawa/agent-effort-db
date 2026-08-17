from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from effort_db import config, schema
from effort_db.cli import app
from effort_db.collectors import github

runner = CliRunner()

# Enterprise Managed User と個人アカウントが混在した端末で実際に出る gh の stderr。
EMU_STDERR = "GraphQL: Unauthorized: As an Enterprise Managed User, you cannot access this content"

# 権限の無いリポジトリは「存在しない」として返るため、名前の誤りと同じ文言になる。
NOT_FOUND_STDERR = "GraphQL: Could not resolve to a Repository with the name 'acme/sample'."

# gh pr list --json ... の出力を模した架空データ（実リポジトリの値は使わない）。
SAMPLE_GH_OUTPUT = json.dumps(
    [
        {
            "number": 12,
            "headRefName": "feat/sample-a",
            "additions": 120,
            "deletions": 30,
            "changedFiles": 4,
            "createdAt": "2026-01-05T01:00:00Z",
            "mergedAt": "2026-01-06T02:00:00Z",
            "labels": [{"name": "enhancement"}, {"name": "収集"}],
            "reviews": [
                {"state": "CHANGES_REQUESTED"},
                {"state": "COMMENTED"},
                {"state": "CHANGES_REQUESTED"},
                {"state": "APPROVED"},
            ],
        },
        {
            "number": 13,
            "additions": 5,
            "deletions": 0,
            "changedFiles": 1,
            "createdAt": "2026-01-07T01:00:00Z",
            "mergedAt": "2026-01-07T03:00:00Z",
            "labels": [],
            "reviews": [{"state": "APPROVED"}],
        },
    ]
)


def _stub_runner(stdout: str, recorder: list[Sequence[str]] | None = None) -> github.GhRunner:
    def run(args: Sequence[str]) -> str:
        if recorder is not None:
            recorder.append(list(args))
        return stdout

    return run


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    schema.init_db(conn)
    return conn


def test_fetch_merged_prs_builds_rows() -> None:
    calls: list[Sequence[str]] = []

    rows = github.fetch_merged_prs(
        "acme/sample", limit=10, runner=_stub_runner(SAMPLE_GH_OUTPUT, calls)
    )

    assert [row.pr_number for row in rows] == [12, 13]
    first = rows[0]
    assert first.repo == "acme/sample"
    assert (first.additions, first.deletions, first.changed_files) == (120, 30, 4)
    assert first.created_at == "2026-01-05T01:00:00Z"
    assert first.merged_at == "2026-01-06T02:00:00Z"
    assert first.head_branch == "feat/sample-a"
    # headRefName を持たない出力では None にする。空文字だと突き合わせで誤って一致する。
    assert rows[1].head_branch is None
    assert json.loads(first.labels) == ["enhancement", "収集"]
    # CHANGES_REQUESTED のみ数えるので 2。APPROVED / COMMENTED は数えない。
    assert first.review_rounds == 2
    assert rows[1].review_rounds == 0
    assert json.loads(rows[1].labels) == []

    args = calls[0]
    # headRefName は突き合わせの主軸。取得列から落ちると join できなくなる。
    assert "headRefName" in args[args.index("--json") + 1]
    assert "--repo" in args and "acme/sample" in args
    assert "--state" in args and "merged" in args
    assert args[args.index("--limit") + 1] == "10"


def test_fetch_merged_prs_treats_empty_array_as_zero_rows() -> None:
    rows = github.fetch_merged_prs("acme/sample", runner=_stub_runner("[]\n"))

    assert rows == []


def test_fetch_merged_prs_rejects_non_json_output() -> None:
    with pytest.raises(github.GhError, match="JSON"):
        github.fetch_merged_prs("acme/sample", runner=_stub_runner("not json"))


def _stub_failing_gh(monkeypatch, *, returncode: int, stderr: str) -> None:
    monkeypatch.setattr(github.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        github.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0], returncode=returncode, stdout="", stderr=stderr
        ),
    )


def test_run_gh_failure_reports_exit_code_and_stderr(monkeypatch) -> None:
    _stub_failing_gh(monkeypatch, returncode=4, stderr="unexpected end of JSON input")

    with pytest.raises(github.GhError) as excinfo:
        github.run_gh(["pr", "list"])

    message = str(excinfo.value)
    assert "exit code 4" in message
    assert "unexpected end of JSON input" in message
    assert "gh auth status" not in message


@pytest.mark.parametrize("stderr", [EMU_STDERR, NOT_FOUND_STDERR])
def test_run_gh_failure_hints_at_account_switching(monkeypatch, stderr: str) -> None:
    _stub_failing_gh(monkeypatch, returncode=1, stderr=stderr)

    with pytest.raises(github.GhError) as excinfo:
        github.run_gh(["pr", "list"])

    message = str(excinfo.value)
    assert stderr in message
    assert "gh auth status" in message


def test_fetch_merged_prs_rejects_row_with_missing_field() -> None:
    broken = json.dumps([{"additions": 1}])

    with pytest.raises(github.GhError, match="PR 情報"):
        github.fetch_merged_prs("acme/sample", runner=_stub_runner(broken))


def test_upsert_is_idempotent_and_updates_values(tmp_path: Path) -> None:
    conn = _open_db(tmp_path / "effort.db")
    try:
        rows = github.fetch_merged_prs("acme/sample", runner=_stub_runner(SAMPLE_GH_OUTPUT))
        github.upsert_pull_requests(conn, rows)
        # linker が後から埋める列。再取り込みで潰されないことを確認する。
        conn.execute(
            "UPDATE pull_requests SET issue_key = ? WHERE repo = ? AND pr_number = ?",
            ("SAMPLE-1", "acme/sample", 12),
        )
        conn.commit()

        updated = [
            replace(
                rows[0],
                review_rounds=3,
                merged_at="2026-01-08T09:00:00Z",
                head_branch="feat/sample-a-renamed",
            ),
            rows[1],
        ]
        github.upsert_pull_requests(conn, updated)

        assert conn.execute("SELECT COUNT(*) FROM pull_requests").fetchone() == (2,)
        assert conn.execute(
            "SELECT merged_at, review_rounds, head_branch, issue_key FROM pull_requests"
            " WHERE repo = ? AND pr_number = ?",
            ("acme/sample", 12),
        ).fetchone() == ("2026-01-08T09:00:00Z", 3, "feat/sample-a-renamed", "SAMPLE-1")
    finally:
        conn.close()


def test_backfill_prs_writes_rows(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "effort.db"
    monkeypatch.setenv(config.ENV_DB_PATH, str(db_path))
    monkeypatch.setattr(github, "run_gh", _stub_runner(SAMPLE_GH_OUTPUT))

    result = runner.invoke(app, ["backfill", "prs", "--repo", "acme/sample"])

    assert result.exit_code == 0
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM pull_requests").fetchone() == (2,)
    finally:
        conn.close()


def test_backfill_prs_warns_when_limit_reached(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_DB_PATH, str(tmp_path / "effort.db"))
    monkeypatch.setattr(github, "run_gh", _stub_runner(SAMPLE_GH_OUTPUT))

    result = runner.invoke(app, ["backfill", "prs", "--repo", "acme/sample", "--limit", "2"])

    assert result.exit_code == 0
    assert "--limit 2 に達しました" in result.output


def test_backfill_prs_fails_with_gh_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_DB_PATH, str(tmp_path / "effort.db"))

    def failing_runner(args: Sequence[str]) -> str:
        raise github.GhError("gh pr list が exit code 1 で失敗しました: Unauthorized")

    monkeypatch.setattr(github, "run_gh", failing_runner)

    result = runner.invoke(app, ["backfill", "prs", "--repo", "acme/sample"])

    assert result.exit_code == 1
    assert "exit code 1" in result.output
