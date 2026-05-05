"""Tests for ``vizio_smartcast.cli._resolve``."""

from __future__ import annotations

from pathlib import Path

import pytest

from vizio_smartcast.cli._config import Config, DeviceRecord
from vizio_smartcast.cli._resolve import (
    CLIResolutionError,
    resolve_device,
)
from vizio_smartcast.types import DeviceType


@pytest.fixture
def empty_cfg(tmp_path: Path) -> Config:
    return Config(path=tmp_path / "config.toml")


@pytest.fixture
def populated_cfg(tmp_path: Path) -> Config:
    cfg = Config(path=tmp_path / "config.toml")
    cfg.add_device(
        DeviceRecord(
            name="tv1",
            host="192.0.2.10",
            device_type=DeviceType.TV,
            auth_token="tok-tv1",
        )
    )
    cfg.add_device(
        DeviceRecord(
            name="speaker",
            host="192.0.2.20",
            device_type=DeviceType.SOUNDBAR,
            auth_token=None,
        )
    )
    return cfg


class TestHostOverride:
    def test_host_only_defaults_to_tv(self, empty_cfg: Config) -> None:
        r = resolve_device(
            host="192.0.2.50",
            device_alias=None,
            device_type=None,
            auth_token=None,
            config=empty_cfg,
        )
        assert r.host == "192.0.2.50"
        assert r.device_type is DeviceType.TV
        assert r.auth_token is None

    def test_host_with_explicit_type(self, empty_cfg: Config) -> None:
        r = resolve_device(
            host="192.0.2.50",
            device_alias=None,
            device_type=DeviceType.SOUNDBAR,
            auth_token="t",
            config=empty_cfg,
        )
        assert r.device_type is DeviceType.SOUNDBAR
        assert r.auth_token == "t"

    def test_host_overrides_alias(self, populated_cfg: Config) -> None:
        # Even with a saved alias and default, --host wins.
        populated_cfg.default_device = "tv1"
        r = resolve_device(
            host="10.0.0.99",
            device_alias="tv1",
            device_type=None,
            auth_token=None,
            config=populated_cfg,
        )
        assert r.host == "10.0.0.99"


class TestAliasResolution:
    def test_alias_resolves(self, populated_cfg: Config) -> None:
        r = resolve_device(
            host=None,
            device_alias="tv1",
            device_type=None,
            auth_token=None,
            config=populated_cfg,
        )
        assert r.host == "192.0.2.10"
        assert r.device_type is DeviceType.TV
        assert r.auth_token == "tok-tv1"

    def test_default_device_used_when_alias_none(self, populated_cfg: Config) -> None:
        populated_cfg.default_device = "speaker"
        r = resolve_device(
            host=None,
            device_alias=None,
            device_type=None,
            auth_token=None,
            config=populated_cfg,
        )
        assert r.host == "192.0.2.20"
        assert r.device_type is DeviceType.SOUNDBAR

    def test_explicit_alias_overrides_default(self, populated_cfg: Config) -> None:
        populated_cfg.default_device = "speaker"
        r = resolve_device(
            host=None,
            device_alias="tv1",
            device_type=None,
            auth_token=None,
            config=populated_cfg,
        )
        assert r.host == "192.0.2.10"

    def test_device_type_override_wins_over_record(self, populated_cfg: Config) -> None:
        r = resolve_device(
            host=None,
            device_alias="tv1",
            device_type=DeviceType.CRAVE_GO,
            auth_token=None,
            config=populated_cfg,
        )
        # Override beats the record's stored type.
        assert r.device_type is DeviceType.CRAVE_GO

    def test_auth_token_override_wins_over_record(self, populated_cfg: Config) -> None:
        r = resolve_device(
            host=None,
            device_alias="tv1",
            device_type=None,
            auth_token="OVERRIDE",
            config=populated_cfg,
        )
        assert r.auth_token == "OVERRIDE"

    def test_empty_string_auth_override_overrides_to_empty(
        self, populated_cfg: Config
    ) -> None:
        # Truthy-vs-not-None: only ``None`` falls back to the record's
        # token. An explicit empty string is treated as a real override.
        r = resolve_device(
            host=None,
            device_alias="tv1",
            device_type=None,
            auth_token="",
            config=populated_cfg,
        )
        assert r.auth_token == ""


class TestErrors:
    def test_no_target_raises(self, empty_cfg: Config) -> None:
        with pytest.raises(CLIResolutionError, match="No device specified"):
            resolve_device(
                host=None,
                device_alias=None,
                device_type=None,
                auth_token=None,
                config=empty_cfg,
            )

    def test_unknown_alias_raises_with_known_list(self, populated_cfg: Config) -> None:
        with pytest.raises(CLIResolutionError) as exc:
            resolve_device(
                host=None,
                device_alias="nope",
                device_type=None,
                auth_token=None,
                config=populated_cfg,
            )
        msg = str(exc.value)
        assert "'nope'" in msg
        # Known aliases listed alphabetically by Config.list_devices().
        assert "speaker" in msg and "tv1" in msg
