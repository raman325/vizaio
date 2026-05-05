"""End-to-end tests for the typer CLI app.

Strategy: mock at the HTTP boundary with ``aioresponses``. Each
invocation runs the real CLI parser, the real ``Vizio`` async client,
the real ``SmartCastClient`` transport, and the real envelope parser
— only the device-side HTTP responses are stubbed. This catches
regressions across the whole stack rather than just the CLI shell.

Where a command's behavior is substantially driven by data not coming
from the device (``discover``, ``pair``), we still mock at the
appropriate boundary (the ``discover`` coroutine, ``pair_session``).
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path

from aioresponses import aioresponses
import pytest
from typer.testing import CliRunner

from tests._fixtures import (
    AUTH_TOKEN,
    TV_HOST_PORT,
    make_battery_level_response,
    make_charging_status_response,
    make_current_app_response,
    make_current_input_response,
    make_device_info_response,
    make_inputs_list_response,
    make_item,
    make_key_press_response,
    make_no_app_response,
    make_pair_begin_response,
    make_pair_finish_response,
    make_power_response,
    make_setting_types_response,
    make_settings_response,
    make_success_response,
)
import vizio_smartcast.cli as cli_module
from vizio_smartcast.cli import app
from vizio_smartcast.cli._config import Config, DeviceRecord
from vizio_smartcast.endpoints import Endpoint, resolve
from vizio_smartcast.types import DeviceType, DiscoveredDevice

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tv_url(endpoint: Endpoint, *, suffix: str = "") -> str:
    """Resolve the TV-profile URL for an endpoint (first path)."""
    spec = resolve(endpoint, DeviceType.TV.profile)
    return f"https://{TV_HOST_PORT}{spec.paths[0]}{suffix}"


def _tv_settings_url(category: str, name: str = "") -> str:
    """Settings root + suffix (the spec's first path)."""
    suffix = f"/{category}"
    if name:
        suffix += f"/{name}"
    return _tv_url(Endpoint.SETTINGS, suffix=suffix)


def _crave_url(endpoint: Endpoint, *, host: str) -> str:
    spec = resolve(endpoint, DeviceType.CRAVE_GO.profile)
    return f"https://{host}{spec.paths[0]}"


def _invoke(runner: CliRunner, cfg_path: Path, *args: str):
    return runner.invoke(app, ["--config", str(cfg_path), *args])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def saved_tv(cfg_path: Path) -> Path:
    """Pre-populate the config file with a TV alias 'tv' at the canonical fixture host."""
    cfg = Config(path=cfg_path)
    cfg.add_device(
        DeviceRecord(
            name="tv",
            host=TV_HOST_PORT,
            device_type=DeviceType.TV,
            auth_token=AUTH_TOKEN,
        )
    )
    cfg.default_device = "tv"
    cfg.save()
    return cfg_path


@pytest.fixture
def mock_aio() -> Iterator[aioresponses]:
    with aioresponses() as m:
        yield m


# ---------------------------------------------------------------------------
# `vizio-smartcast device …` (config-only)
# ---------------------------------------------------------------------------


class TestDeviceSubcommand:
    def test_add_writes_record(self, runner: CliRunner, cfg_path: Path) -> None:
        result = _invoke(
            runner,
            cfg_path,
            "device",
            "add",
            "tv1",
            "--host",
            "192.0.2.10",
            "--device-type",
            "tv",
            "--auth",
            "tok",
        )
        assert result.exit_code == 0, result.output
        cfg = Config.load(cfg_path)
        assert cfg.get_device("tv1").host == "192.0.2.10"
        assert cfg.get_device("tv1").auth_token == "tok"

    def test_remove(self, runner: CliRunner, saved_tv: Path) -> None:
        result = _invoke(runner, saved_tv, "device", "remove", "tv")
        assert result.exit_code == 0
        assert "tv" not in Config.load(saved_tv)

    def test_list_shows_default_marker(self, runner: CliRunner, saved_tv: Path) -> None:
        result = _invoke(runner, saved_tv, "device", "list", "--format", "tsv")
        assert result.exit_code == 0
        # default=True renders as '*'; auth presence renders as 'yes'.
        assert f"tv\t{TV_HOST_PORT}\ttv\t*\tyes" in result.output

    def test_set_default_unknown_alias_exits_2(
        self, runner: CliRunner, cfg_path: Path
    ) -> None:
        result = _invoke(runner, cfg_path, "device", "set-default", "nope")
        assert result.exit_code == 2

    def test_set_default_persists(self, runner: CliRunner, cfg_path: Path) -> None:
        _invoke(
            runner,
            cfg_path,
            "device",
            "add",
            "a",
            "--host",
            "1.1.1.1",
            "--device-type",
            "tv",
        )
        result = _invoke(runner, cfg_path, "device", "set-default", "a")
        assert result.exit_code == 0
        assert Config.load(cfg_path).default_device == "a"

    def test_show_default(self, runner: CliRunner, saved_tv: Path) -> None:
        result = _invoke(runner, saved_tv, "device", "show", "--format", "tsv")
        assert result.exit_code == 0
        assert TV_HOST_PORT in result.output

    def test_show_explicit_alias(self, runner: CliRunner, saved_tv: Path) -> None:
        result = _invoke(runner, saved_tv, "device", "show", "tv", "--format", "tsv")
        assert result.exit_code == 0
        assert TV_HOST_PORT in result.output

    def test_show_no_default_no_arg_exits_2(
        self, runner: CliRunner, cfg_path: Path
    ) -> None:
        result = _invoke(runner, cfg_path, "device", "show")
        assert result.exit_code == 2

    def test_show_unknown_alias_exits_2(
        self, runner: CliRunner, cfg_path: Path
    ) -> None:
        result = _invoke(runner, cfg_path, "device", "show", "nope")
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Resolution-error path (no device target, even before HTTP)
# ---------------------------------------------------------------------------


class TestResolutionErrors:
    def test_no_target_exits_2(self, runner: CliRunner, cfg_path: Path) -> None:
        result = _invoke(runner, cfg_path, "power", "state")
        assert result.exit_code == 2

    def test_unknown_alias_exits_2(self, runner: CliRunner, saved_tv: Path) -> None:
        result = _invoke(runner, saved_tv, "--device", "nope", "power", "state")
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------


class TestPowerCommands:
    def test_state_on(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(_tv_url(Endpoint.POWER_MODE), payload=make_power_response(1))
        result = _invoke(runner, saved_tv, "--format", "plain", "power", "state")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "on"

    def test_state_off(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(_tv_url(Endpoint.POWER_MODE), payload=make_power_response(0))
        result = _invoke(runner, saved_tv, "--format", "plain", "power", "state")
        assert result.exit_code == 0
        assert "off" in result.output

    def test_on(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # ``power on`` sends a key_press; mock the PUT.
        mock_aio.put(_tv_url(Endpoint.KEY_PRESS), payload=make_key_press_response())
        result = _invoke(runner, saved_tv, "power", "on")
        assert result.exit_code == 0, result.output

    def test_off(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.put(_tv_url(Endpoint.KEY_PRESS), payload=make_key_press_response())
        result = _invoke(runner, saved_tv, "power", "off")
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Volume / mute
# ---------------------------------------------------------------------------


class TestVolumeCommands:
    def test_level(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # ``get_volume`` reads the audio.volume setting.
        mock_aio.get(
            _tv_settings_url("audio", "volume"),
            payload=make_settings_response(
                [("volume", 17, "T_VALUE_ABS_V1", 1)],
            ),
        )
        result = _invoke(runner, saved_tv, "volume", "level", "--format", "plain")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "17"

    def test_up_with_steps(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # 5 vol_up keypresses; let the mock match all of them.
        mock_aio.put(
            _tv_url(Endpoint.KEY_PRESS),
            payload=make_key_press_response(),
            repeat=True,
        )
        result = _invoke(runner, saved_tv, "volume", "up", "--steps", "5")
        assert result.exit_code == 0, result.output

    def test_down_default_steps(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.put(_tv_url(Endpoint.KEY_PRESS), payload=make_key_press_response())
        result = _invoke(runner, saved_tv, "volume", "down")
        assert result.exit_code == 0

    def test_mute_when_already_muted(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # mute() probes audio.mute first; if already 'On', it no-ops.
        mock_aio.get(
            _tv_settings_url("audio", "mute"),
            payload=make_settings_response(
                [("mute", "On", "T_LIST_V1", 1)],
            ),
        )
        result = _invoke(runner, saved_tv, "volume", "mute")
        assert result.exit_code == 0

    def test_unmute_when_already_unmuted(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(
            _tv_settings_url("audio", "mute"),
            payload=make_settings_response(
                [("mute", "Off", "T_LIST_V1", 1)],
            ),
        )
        result = _invoke(runner, saved_tv, "volume", "unmute")
        assert result.exit_code == 0

    def test_max_uses_profile_no_http(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # ``volume max`` is a static profile lookup — must not hit HTTP.
        result = _invoke(runner, saved_tv, "volume", "max", "--format", "plain")
        assert result.exit_code == 0
        assert int(result.output.strip()) > 0


# ---------------------------------------------------------------------------
# Inputs / remote keys
# ---------------------------------------------------------------------------


class TestInputCommands:
    def test_list(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # ``get_inputs`` is satisfied entirely by the inputs list response —
        # the synthetic ``current_input`` entry inside it marks which input
        # is selected (no separate /current_input fetch needed).
        mock_aio.get(
            _tv_url(Endpoint.INPUTS),
            payload=make_inputs_list_response(
                [
                    ("hdmi1", "HDMI-1", "PS5", 5),
                    ("cast", "CAST", "SMARTCAST", 6),
                ],
                current_input_meta_name="PS5",
            ),
        )
        result = _invoke(runner, saved_tv, "input", "list", "--format", "tsv")
        assert result.exit_code == 0, result.output
        # current=True for the matching input; '*' marker on the row.
        assert "HDMI-1\tPS5\t*" in result.output
        assert "CAST\tSMARTCAST\t" in result.output

    def test_current(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(
            _tv_url(Endpoint.CURRENT_INPUT),
            payload=make_current_input_response("current_input", "HDMI-1", 5),
        )
        result = _invoke(runner, saved_tv, "input", "current", "--format", "plain")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "HDMI-1"

    def test_set(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(
            _tv_url(Endpoint.INPUTS),
            payload=make_inputs_list_response(
                [("hdmi1", "HDMI-1", "", 5), ("cast", "CAST", "SMARTCAST", 6)],
                current_input_meta_name="SMARTCAST",
            ),
        )
        mock_aio.get(
            _tv_url(Endpoint.CURRENT_INPUT),
            payload=make_current_input_response("current_input", "SMARTCAST", 6),
        )
        mock_aio.put(
            _tv_url(Endpoint.CURRENT_INPUT),
            payload=make_success_response(),
        )
        result = _invoke(runner, saved_tv, "input", "set", "HDMI-1")
        assert result.exit_code == 0, result.output

    def test_set_invalid_input_exits_1(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(
            _tv_url(Endpoint.INPUTS),
            payload=make_inputs_list_response(
                [("hdmi1", "HDMI-1", "", 5)],
                current_input_meta_name="HDMI-1",
            ),
        )
        result = _invoke(runner, saved_tv, "input", "set", "HDMI-99")
        assert result.exit_code == 1

    def test_next(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # next_input is a single key press.
        mock_aio.put(_tv_url(Endpoint.KEY_PRESS), payload=make_key_press_response())
        result = _invoke(runner, saved_tv, "input", "next")
        assert result.exit_code == 0


class TestRemoteCommands:
    def test_send(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.put(_tv_url(Endpoint.KEY_PRESS), payload=make_key_press_response())
        result = _invoke(runner, saved_tv, "remote", "send", "MENU")
        assert result.exit_code == 0

    def test_keys_lists_profile_no_http(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # Static profile lookup — must not hit HTTP.
        result = _invoke(runner, saved_tv, "remote", "keys", "--format", "tsv")
        assert result.exit_code == 0
        assert "POW_ON" in result.output


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettingsCommands:
    def test_types(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(
            _tv_url(Endpoint.SETTINGS),
            payload=make_setting_types_response(["audio", "picture"]),
        )
        result = _invoke(runner, saved_tv, "settings", "types", "--format", "tsv")
        assert result.exit_code == 0, result.output
        assert "audio" in result.output and "picture" in result.output

    def test_list(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(
            _tv_settings_url("audio"),
            payload=make_settings_response(
                [
                    ("volume", 17, "T_VALUE_ABS_V1", 1),
                    ("mute", "Off", "T_LIST_V1", 2),
                ],
            ),
        )
        result = _invoke(
            runner, saved_tv, "settings", "list", "audio", "--format", "tsv"
        )
        assert result.exit_code == 0, result.output
        assert "volume\t17" in result.output
        assert "mute\tOff" in result.output

    def test_get(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(
            _tv_settings_url("audio", "volume"),
            payload=make_settings_response(
                [("volume", 17, "T_VALUE_ABS_V1", 1)],
            ),
        )
        result = _invoke(
            runner,
            saved_tv,
            "settings",
            "get",
            "audio",
            "volume",
            "--format",
            "plain",
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "17"

    def test_set_int_value_coerced(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # set_setting fetches the setting first to get the hashval, then PUTs.
        mock_aio.get(
            _tv_settings_url("audio", "volume"),
            payload=make_settings_response(
                [("volume", 17, "T_VALUE_ABS_V1", 42)],
            ),
        )
        mock_aio.put(
            _tv_settings_url("audio", "volume"),
            payload=make_success_response(),
        )
        result = _invoke(runner, saved_tv, "settings", "set", "audio", "volume", "30")
        assert result.exit_code == 0, result.output
        # Verify the PUT body contained the int (coerced from "30").
        put_calls = [
            req
            for (method, _), reqs in mock_aio.requests.items()
            if method == "PUT"
            for req in reqs
        ]
        assert put_calls
        body = json.loads(put_calls[0].kwargs["data"])
        # Settings PUT body: {"VALUE": v, "HASHVAL": h, "REQUEST": "MODIFY"}.
        assert body["VALUE"] == 30
        assert body["REQUEST"] == "MODIFY"
        assert body["HASHVAL"] == 42


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class TestAppCommands:
    def test_current(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(
            _tv_url(Endpoint.CURRENT_APP),
            payload=make_current_app_response(app_id="3", name_space=2, message=None),
        )
        result = _invoke(runner, saved_tv, "app", "current", "--format", "plain")
        assert result.exit_code == 0, result.output
        # Output is the app name from the bundled catalog (or fallback).
        assert result.output.strip() != ""

    def test_current_none_renders_placeholder(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(
            _tv_url(Endpoint.CURRENT_APP),
            payload=make_no_app_response(),
        )
        result = _invoke(runner, saved_tv, "app", "current", "--format", "plain")
        assert result.exit_code == 0
        assert "(no app running)" in result.output

    def test_launch(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.put(
            _tv_url(Endpoint.LAUNCH_APP),
            payload=make_success_response(),
        )
        result = _invoke(runner, saved_tv, "app", "launch", "Netflix")
        assert result.exit_code == 0, result.output

    def test_launch_config(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.put(
            _tv_url(Endpoint.LAUNCH_APP),
            payload=make_success_response(),
        )
        result = _invoke(
            runner,
            saved_tv,
            "app",
            "launch-config",
            "5",
            "2",
            '{"k":"v"}',
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Identity (info)
# ---------------------------------------------------------------------------


class TestInfoCommands:
    def _aggregate(self) -> dict:
        # Modern firmware exposes identity fields under a single
        # ``tv_information`` parent (cname-keyed items).
        return make_success_response(
            items=[
                make_item("serial_number", "SN123", item_type="T_STRING_V1"),
                make_item("esn", "ESN999", item_type="T_STRING_V1"),
                make_item("version", "3.720.9.1-1", item_type="T_STRING_V1"),
            ]
        )

    def test_model(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # ``get_model_name`` reads ``Endpoint.DEVICE_INFO``, which has a
        # nested settings_root → model_name path inside ITEMS[0].VALUE.
        mock_aio.get(
            _tv_url(Endpoint.DEVICE_INFO),
            payload=make_device_info_response({"model_name": "VHD24M-0810"}),
        )
        result = _invoke(runner, saved_tv, "info", "model", "--format", "plain")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "VHD24M-0810"

    def test_serial(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(_tv_url(Endpoint.TV_INFORMATION), payload=self._aggregate())
        result = _invoke(runner, saved_tv, "info", "serial", "--format", "plain")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "SN123"

    def test_esn(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(_tv_url(Endpoint.TV_INFORMATION), payload=self._aggregate())
        result = _invoke(runner, saved_tv, "info", "esn", "--format", "plain")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "ESN999"

    def test_version(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(_tv_url(Endpoint.TV_INFORMATION), payload=self._aggregate())
        result = _invoke(runner, saved_tv, "info", "version", "--format", "plain")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "3.720.9.1-1"

    def test_all(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        # ``info all`` uses get_device_info, which calls get_model_name
        # (DEVICE_INFO), the identity aggregate (TV_INFORMATION), and
        # get_inputs (INPUTS, with a synthetic current_input row).
        mock_aio.get(_tv_url(Endpoint.TV_INFORMATION), payload=self._aggregate())
        mock_aio.get(
            _tv_url(Endpoint.DEVICE_INFO),
            payload=make_device_info_response({"model_name": "VHD24M-0810"}),
        )
        mock_aio.get(
            _tv_url(Endpoint.INPUTS),
            payload=make_inputs_list_response(
                [("hdmi1", "HDMI-1", "", 5)],
                current_input_meta_name="HDMI-1",
            ),
        )
        result = _invoke(runner, saved_tv, "info", "all", "--format", "tsv")
        assert result.exit_code == 0, result.output
        assert "VHD24M-0810" in result.output
        assert "HDMI-1" in result.output


# ---------------------------------------------------------------------------
# Battery (Crave only)
# ---------------------------------------------------------------------------


class TestBatteryCommands:
    def test_level(
        self, runner: CliRunner, cfg_path: Path, mock_aio: aioresponses
    ) -> None:
        crave_host = "192.0.2.30:9000"
        mock_aio.get(
            _crave_url(Endpoint.BATTERY_LEVEL, host=crave_host),
            payload=make_battery_level_response(73),
        )
        result = _invoke(
            runner,
            cfg_path,
            "--host",
            crave_host,
            "--device-type",
            "crave_go",
            "battery",
            "level",
            "--format",
            "plain",
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "73"

    def test_charging(
        self, runner: CliRunner, cfg_path: Path, mock_aio: aioresponses
    ) -> None:
        crave_host = "192.0.2.30:9000"
        mock_aio.get(
            _crave_url(Endpoint.CHARGING_STATUS, host=crave_host),
            payload=make_charging_status_response(1),
        )
        result = _invoke(
            runner,
            cfg_path,
            "--host",
            crave_host,
            "--device-type",
            "crave_go",
            "battery",
            "charging",
            "--format",
            "plain",
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "charging"


# ---------------------------------------------------------------------------
# VizioError surfacing — driven by a real device-side error envelope
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    def test_envelope_error_exits_1_with_prefix(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(
            _tv_url(Endpoint.POWER_MODE),
            payload={
                "STATUS": {
                    "RESULT": "REQUIRES_PAIRING",
                    "DETAIL": "token rejected",
                }
            },
        )
        result = _invoke(runner, saved_tv, "power", "state")
        assert result.exit_code == 1
        assert "vizio-smartcast" in result.output

    def test_http_403_exits_1(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(_tv_url(Endpoint.POWER_MODE), status=403, body="")
        result = _invoke(runner, saved_tv, "power", "state")
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# `vizio-smartcast discover` — mocked at the discover() coroutine boundary
# ---------------------------------------------------------------------------


class TestDiscoverCommand:
    def test_discover_returns_rows(
        self, runner: CliRunner, cfg_path: Path, monkeypatch
    ) -> None:
        async def fake_discover(*, timeout, include_ssdp):
            assert timeout == 7.0
            assert include_ssdp is False
            return [
                DiscoveredDevice(
                    name="Living Room TV",
                    ip="192.0.2.10",
                    port=7345,
                    model="VHD24M-0810",
                    id="abc123",
                ),
            ]

        monkeypatch.setattr(cli_module, "discover", fake_discover)
        result = _invoke(
            runner,
            cfg_path,
            "discover",
            "--timeout",
            "7",
            "--no-ssdp",
            "--format",
            "tsv",
        )
        assert result.exit_code == 0
        assert "Living Room TV" in result.output
        assert "192.0.2.10:7345" in result.output

    def test_discover_no_results_exits_1(
        self, runner: CliRunner, cfg_path: Path, monkeypatch
    ) -> None:
        async def fake_discover(*, timeout, include_ssdp):
            return []

        monkeypatch.setattr(cli_module, "discover", fake_discover)
        result = _invoke(runner, cfg_path, "discover")
        assert result.exit_code == 1
        assert "No Vizio devices found" in result.output


# ---------------------------------------------------------------------------
# `vizio-smartcast pair` — mocked at the HTTP boundary
# ---------------------------------------------------------------------------


class TestPairCommand:
    def test_pair_prints_token_when_no_save_as(
        self, runner: CliRunner, cfg_path: Path, mock_aio: aioresponses
    ) -> None:
        host = "192.0.2.42"
        mock_aio.put(
            f"https://{host}{resolve(Endpoint.BEGIN_PAIR, DeviceType.TV.profile).paths[0]}",
            payload=make_pair_begin_response(challenge_type=1, token=99),
        )
        mock_aio.put(
            f"https://{host}{resolve(Endpoint.FINISH_PAIR, DeviceType.TV.profile).paths[0]}",
            payload=make_pair_finish_response(auth_token="TOK-XYZ"),
        )
        result = runner.invoke(
            app,
            ["--config", str(cfg_path), "pair", host, "--format", "json"],
            input="1234\n",
        )
        assert result.exit_code == 0, result.output
        assert "TOK-XYZ" in result.output

    def test_pair_saves_when_save_as(
        self, runner: CliRunner, cfg_path: Path, mock_aio: aioresponses
    ) -> None:
        host = "192.0.2.42"
        mock_aio.put(
            f"https://{host}{resolve(Endpoint.BEGIN_PAIR, DeviceType.TV.profile).paths[0]}",
            payload=make_pair_begin_response(challenge_type=1, token=99),
        )
        mock_aio.put(
            f"https://{host}{resolve(Endpoint.FINISH_PAIR, DeviceType.TV.profile).paths[0]}",
            payload=make_pair_finish_response(auth_token="TOK-SAVED"),
        )
        result = runner.invoke(
            app,
            [
                "--config",
                str(cfg_path),
                "pair",
                host,
                "--save-as",
                "tvalias",
            ],
            input="0000\n",
        )
        assert result.exit_code == 0, result.output
        cfg = Config.load(cfg_path)
        assert cfg.get_device("tvalias").host == host
        assert cfg.get_device("tvalias").auth_token == "TOK-SAVED"

    def test_pair_failure_exits_1(
        self, runner: CliRunner, cfg_path: Path, mock_aio: aioresponses
    ) -> None:
        host = "192.0.2.42"
        mock_aio.put(
            f"https://{host}{resolve(Endpoint.BEGIN_PAIR, DeviceType.TV.profile).paths[0]}",
            payload=make_pair_begin_response(challenge_type=1, token=99),
        )
        # finish_pair returns PAIRING_DENIED — surfaces as VizioAuthError.
        mock_aio.put(
            f"https://{host}{resolve(Endpoint.FINISH_PAIR, DeviceType.TV.profile).paths[0]}",
            payload={"STATUS": {"RESULT": "PAIRING_DENIED", "DETAIL": "wrong PIN"}},
        )
        # The pair_session's exit will cancel; mock that too.
        mock_aio.put(
            f"https://{host}/pairing/cancel",
            payload=make_success_response(),
            repeat=True,
        )
        result = runner.invoke(
            app,
            ["--config", str(cfg_path), "pair", host],
            input="9999\n",
        )
        assert result.exit_code == 1
        assert "Pairing failed" in result.output


# ---------------------------------------------------------------------------
# Verbose flag
# ---------------------------------------------------------------------------


class TestVerboseFlag:
    def test_verbose_enables_debug_logging(
        self, runner: CliRunner, saved_tv: Path, mock_aio: aioresponses
    ) -> None:
        mock_aio.get(_tv_url(Endpoint.POWER_MODE), payload=make_power_response(1))
        result = _invoke(runner, saved_tv, "-v", "power", "state")
        assert result.exit_code == 0
