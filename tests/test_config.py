from __future__ import annotations

from pathlib import Path

from effort_db import config


def test_env_var_has_highest_priority(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "nested" / "custom.db"
    monkeypatch.setenv(config.ENV_DB_PATH, str(db_path))

    resolved = config.resolve_db_path(data_dir=tmp_path / "unused")

    assert resolved == db_path
    assert resolved.parent.is_dir()


def test_config_toml_used_when_env_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(config.ENV_DB_PATH, raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    configured_path = tmp_path / "from_config" / "effort.db"
    (data_dir / config.CONFIG_FILE_NAME).write_text(f'db_path = "{configured_path}"\n')

    resolved = config.resolve_db_path(data_dir=data_dir)

    assert resolved == configured_path
    assert resolved.parent.is_dir()


def test_default_path_when_no_env_and_no_config_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(config.ENV_DB_PATH, raising=False)
    data_dir = tmp_path / "data"

    resolved = config.resolve_db_path(data_dir=data_dir)

    assert resolved == data_dir / config.DEFAULT_DB_NAME
    assert not (data_dir / config.CONFIG_FILE_NAME).exists()
    assert resolved.parent.is_dir()
