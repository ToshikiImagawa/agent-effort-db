"""DBパス等の設定解決。

優先順位: 環境変数 EFFORT_DB_PATH > config.toml > デフォルトパス。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

ENV_DB_PATH = "EFFORT_DB_PATH"
DEFAULT_DATA_DIR = Path.home() / ".claude" / "plugins" / "data" / "effort-db"
DEFAULT_DB_NAME = "effort.db"
CONFIG_FILE_NAME = "config.toml"


def resolve_db_path(*, data_dir: Path | None = None) -> Path:
    """DBファイルパスを解決する。

    `data_dir` はデフォルト値（DEFAULT_DATA_DIR）を上書きするためのフックで、
    config.toml の探索先とデフォルトDBパスの両方に影響する。テストからの注入用。
    """
    env_value = os.environ.get(ENV_DB_PATH)
    if env_value:
        return _ensure_parent(Path(env_value).expanduser())

    base_dir = data_dir if data_dir is not None else DEFAULT_DATA_DIR
    config_path = base_dir / CONFIG_FILE_NAME
    if config_path.is_file():
        with config_path.open("rb") as f:
            config = tomllib.load(f)
        configured = config.get("db_path")
        if configured:
            return _ensure_parent(Path(configured).expanduser())

    return _ensure_parent(base_dir / DEFAULT_DB_NAME)


def _ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
