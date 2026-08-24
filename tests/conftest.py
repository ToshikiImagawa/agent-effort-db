"""テスト全体で共有する fixture。"""

from __future__ import annotations

import pytest

from effort_db import config


@pytest.fixture(autouse=True)
def _unset_claude_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE_CONFIG_DIR が未設定の状態を既定にする。

    実行環境（Claude Code のプラグイン/スキルからの起動等）で設定されていても、
    既存の monkeypatch.setattr(module, "DEFAULT_*", ...) パターンのテストが
    環境変数に引っ張られないようにする。CLAUDE_CONFIG_DIR の挙動そのものを検証する
    テストは、この fixture の後で monkeypatch.setenv(...) すれば上書きできる。
    """
    monkeypatch.delenv(config.ENV_CLAUDE_CONFIG_DIR, raising=False)
