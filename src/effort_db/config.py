"""DBパス等の設定解決。

優先順位: 環境変数 EFFORT_DB_PATH > config.toml > デフォルトパス。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

ENV_DB_PATH = "EFFORT_DB_PATH"
DEFAULT_DATA_DIR = Path.home() / ".claude" / "plugins" / "data" / "effort-db"
DEFAULT_DB_NAME = "effort.db"
CONFIG_FILE_NAME = "config.toml"
CONFIG_KEY_ISSUE_KEY_PATTERNS = "issue_key_patterns"


def resolve_db_path(*, data_dir: Path | None = None) -> Path:
    """DBファイルパスを解決する。

    `data_dir` はデフォルト値（DEFAULT_DATA_DIR）を上書きするためのフックで、
    config.toml の探索先とデフォルトDBパスの両方に影響する。テストからの注入用。
    """
    env_value = os.environ.get(ENV_DB_PATH)
    if env_value:
        return _ensure_parent(Path(env_value).expanduser())

    base_dir = data_dir if data_dir is not None else DEFAULT_DATA_DIR
    configured = _load_config(base_dir).get("db_path")
    if configured:
        return _ensure_parent(Path(configured).expanduser())

    return _ensure_parent(base_dir / DEFAULT_DB_NAME)


def load_issue_key_patterns(*, data_dir: Path | None = None) -> list[str] | None:
    """config.toml の issue_key_patterns（正規表現の配列）を返す。未設定なら None。

    既定パターンをここに持たないのは、社内固有のチケットキー接頭辞を設定ファイル
    側だけに閉じ込め、ソースには汎用パターンのみを置くため（既定値は linker が持つ）。
    """
    base_dir = data_dir if data_dir is not None else DEFAULT_DATA_DIR
    patterns = _load_config(base_dir).get(CONFIG_KEY_ISSUE_KEY_PATTERNS)
    if patterns is None:
        return None
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise ValueError(f"{CONFIG_KEY_ISSUE_KEY_PATTERNS} は文字列の配列で指定してください")
    return list(patterns)


def _load_config(base_dir: Path) -> dict[str, Any]:
    config_path = base_dir / CONFIG_FILE_NAME
    if not config_path.is_file():
        return {}
    with config_path.open("rb") as f:
        return tomllib.load(f)


def _ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
