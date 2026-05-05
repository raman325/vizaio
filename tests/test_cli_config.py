"""Tests for ``vizio_smartcast.cli._config`` — file persistence and recovery.

Uses ``tmp_path`` rather than mocking, so the round-trip exercises real
``tomlkit`` parse/serialize.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vizio_smartcast.cli._config import (
    Config,
    DeviceRecord,
    default_config_path,
)
from vizio_smartcast.types import DeviceType


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def _record(name: str = "tv", host: str = "192.0.2.10:7345") -> DeviceRecord:
    return DeviceRecord(
        name=name,
        host=host,
        device_type=DeviceType.TV,
        auth_token="tok-" + name,
    )


class TestDefaultConfigPath:
    def test_env_override_wins(self, monkeypatch, tmp_path: Path) -> None:
        target = tmp_path / "alt" / "myconfig.toml"
        monkeypatch.setenv("VIZIO_SMARTCAST_CONFIG", str(target))
        assert default_config_path() == target

    def test_falls_back_to_platformdirs(self, monkeypatch) -> None:
        # Path varies per platform; assert only that an env-override does
        # NOT leak through and the result is a Path.
        monkeypatch.delenv("VIZIO_SMARTCAST_CONFIG", raising=False)
        result = default_config_path()
        assert isinstance(result, Path)
        assert result.name == "config.toml"


class TestConfigLoadEmpty:
    def test_missing_file_returns_empty_config(self, cfg_path: Path) -> None:
        assert not cfg_path.exists()
        cfg = Config.load(cfg_path)
        assert cfg.list_devices() == []
        assert cfg.default_device is None

    def test_default_path_used_when_none_passed(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "default.toml"
        monkeypatch.setenv("VIZIO_SMARTCAST_CONFIG", str(target))
        cfg = Config.load()
        assert cfg.path == target
        assert cfg.list_devices() == []


class TestConfigRoundTrip:
    def test_save_then_load(self, cfg_path: Path) -> None:
        cfg = Config(path=cfg_path)
        cfg.add_device(_record("tv1", "192.0.2.10"))
        cfg.add_device(_record("tv2", "192.0.2.11"))
        cfg.default_device = "tv1"
        cfg.save()

        loaded = Config.load(cfg_path)
        assert loaded.default_device == "tv1"
        names = [r.name for r in loaded.list_devices()]
        assert names == ["tv1", "tv2"]
        assert loaded.get_device("tv1").host == "192.0.2.10"
        assert loaded.get_device("tv1").auth_token == "tok-tv1"

    def test_save_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "config.toml"
        cfg = Config(path=nested)
        cfg.add_device(_record())
        cfg.save()
        assert nested.exists()

    def test_save_sets_0600_on_unix(self, cfg_path: Path) -> None:
        if os.name == "nt":
            pytest.skip("chmod semantics differ on Windows")
        cfg = Config(path=cfg_path)
        cfg.add_device(_record())
        cfg.save()
        mode = cfg_path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_save_omits_empty_auth_token(self, cfg_path: Path) -> None:
        cfg = Config(path=cfg_path)
        cfg.add_device(
            DeviceRecord(
                name="speaker",
                host="192.0.2.20",
                device_type=DeviceType.SOUNDBAR,
                auth_token=None,
            )
        )
        cfg.save()
        content = cfg_path.read_text()
        assert "auth_token" not in content


class TestConfigLoadRecovery:
    def test_corrupt_toml_returns_empty(self, cfg_path: Path) -> None:
        cfg_path.write_text("not = valid = toml\n[\n")
        cfg = Config.load(cfg_path)
        assert cfg.list_devices() == []
        assert cfg.default_device is None

    def test_skips_malformed_device_entry(self, cfg_path: Path) -> None:
        # Missing 'host' on tv2 — should be skipped, tv1 preserved.
        cfg_path.write_text(
            'default_device = "tv1"\n'
            "[devices.tv1]\n"
            'host = "192.0.2.10"\n'
            'device_type = "tv"\n'
            "[devices.tv2]\n"
            'device_type = "tv"\n'
        )
        cfg = Config.load(cfg_path)
        names = [r.name for r in cfg.list_devices()]
        assert names == ["tv1"]
        assert cfg.default_device == "tv1"

    def test_skips_invalid_device_type(self, cfg_path: Path) -> None:
        cfg_path.write_text(
            '[devices.tv1]\nhost = "192.0.2.10"\ndevice_type = "not-a-real-type"\n'
        )
        cfg = Config.load(cfg_path)
        assert cfg.list_devices() == []


class TestConfigMutators:
    def test_remove_device_clears_default_when_matching(self, cfg_path: Path) -> None:
        cfg = Config(path=cfg_path)
        cfg.add_device(_record("tv1"))
        cfg.default_device = "tv1"
        cfg.remove_device("tv1")
        assert cfg.default_device is None
        assert cfg.list_devices() == []

    def test_remove_device_keeps_default_when_other(self, cfg_path: Path) -> None:
        cfg = Config(path=cfg_path)
        cfg.add_device(_record("tv1"))
        cfg.add_device(_record("tv2"))
        cfg.default_device = "tv1"
        cfg.remove_device("tv2")
        assert cfg.default_device == "tv1"

    def test_remove_unknown_is_noop(self, cfg_path: Path) -> None:
        cfg = Config(path=cfg_path)
        cfg.add_device(_record("tv1"))
        cfg.remove_device("nope")
        assert [r.name for r in cfg.list_devices()] == ["tv1"]

    def test_get_device_unknown_raises(self, cfg_path: Path) -> None:
        cfg = Config(path=cfg_path)
        with pytest.raises(KeyError):
            cfg.get_device("nope")

    def test_contains(self, cfg_path: Path) -> None:
        cfg = Config(path=cfg_path)
        cfg.add_device(_record("tv1"))
        assert "tv1" in cfg
        assert "nope" not in cfg

    def test_with_device_returns_self(self, cfg_path: Path) -> None:
        cfg = Config(path=cfg_path)
        result = cfg.with_device(_record("tv1"))
        assert result is cfg
        assert "tv1" in cfg

    def test_update_device_changes_fields(self, cfg_path: Path) -> None:
        cfg = Config(path=cfg_path)
        cfg.add_device(_record("tv1"))
        new = cfg.update_device("tv1", host="192.0.2.99")
        assert new.host == "192.0.2.99"
        assert cfg.get_device("tv1").host == "192.0.2.99"
        # Other fields preserved.
        assert cfg.get_device("tv1").auth_token == "tok-tv1"

    def test_update_device_unknown_raises(self, cfg_path: Path) -> None:
        cfg = Config(path=cfg_path)
        with pytest.raises(KeyError):
            cfg.update_device("nope", host="x")
