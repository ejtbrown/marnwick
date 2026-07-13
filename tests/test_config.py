from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import marnwick.config as config_module
from marnwick.config import AppConfig, WindowConfig, load_config, save_config


def test_load_config_tolerates_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b'{"catalogs":["ok"]}\xff')

    assert load_config(path) == AppConfig()


@pytest.mark.parametrize("catalogs", ["one", 1, True, {}, None])
def test_load_config_rejects_non_list_catalog_values(tmp_path: Path, catalogs: object) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"catalogs": catalogs}), encoding="utf-8")

    assert load_config(path).catalogs == []


def test_load_config_defaults_unexpected_scalar_types(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "window": {
                    "x": [],
                    "y": {},
                    "width": [],
                    "height": {},
                    "maximized": "false",
                },
                "catalogs": ["one", 2, None, "two"],
                "thumbnail_size": {},
                "delete_behavior": [],
                "sort_order": ["date"],
            }
        ),
        encoding="utf-8",
    )

    assert load_config(path) == AppConfig(catalogs=["one", "two"])


def test_save_config_is_atomic_and_preserves_existing_mode(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"old": true}\n', encoding="utf-8")
    path.chmod(0o640)
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(config_module.os, "fsync", recording_fsync)

    save_config(AppConfig(window=WindowConfig(width=900, height=700), catalogs=["photos"]), path)

    assert load_config(path).catalogs == ["photos"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert fsync_calls
    assert not list(tmp_path.glob(".config.json.*.tmp"))


def test_save_config_uses_private_mode_for_new_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    save_config(AppConfig(), path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_config_replace_failure_preserves_original(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    original = b'{"old": true}\n'
    path.write_bytes(original)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        save_config(AppConfig(catalogs=["new"]), path)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".config.json.*.tmp"))


def test_stale_process_configs_merge_independent_catalog_additions(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(AppConfig(catalogs=["base"]), path)
    first = load_config(path)
    second = load_config(path)

    first.catalogs.append("first")
    save_config(first, path)
    second.catalogs.append("second")
    save_config(second, path)

    assert load_config(path).catalogs == ["base", "first", "second"]


def test_stale_process_config_does_not_resurrect_catalog_removed_elsewhere(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(AppConfig(catalogs=["keep", "remove"]), path)
    remover = load_config(path)
    unchanged = load_config(path)

    remover.catalogs.remove("remove")
    save_config(remover, path)
    save_config(unchanged, path)

    assert load_config(path).catalogs == ["keep"]


def test_save_config_refuses_symlink_lock_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    target = tmp_path / "unrelated"
    target.write_bytes(b"unchanged")
    path.with_name("config.json.lock").symlink_to(target)

    with pytest.raises(OSError, match="lock must not be a symbolic link"):
        save_config(AppConfig(catalogs=["photos"]), path)

    assert target.read_bytes() == b"unchanged"
    assert not path.exists()


def test_stale_process_does_not_erase_catalogs_when_config_becomes_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(AppConfig(catalogs=["one", "two"]), path)
    loaded = load_config(path)
    path.write_bytes(b"{partial")

    save_config(loaded, path)

    assert load_config(path).catalogs == ["one", "two"]


def test_save_config_refuses_hard_linked_lock_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    target = tmp_path / "unrelated"
    target.write_bytes(b"unchanged")
    os.link(target, path.with_name("config.json.lock"))

    with pytest.raises(OSError, match="lock must not be hard-linked"):
        save_config(AppConfig(catalogs=["photos"]), path)

    assert target.read_bytes() == b"unchanged"
    assert not path.exists()
