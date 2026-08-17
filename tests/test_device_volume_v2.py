"""Device-layer volume behavior on volume-V2 firmware.

Covers three things the older code path got wrong on newer TVs:

1. **Capability detection** — ``API_VERSION`` from the cached deviceinfo
   payload decides whether the flat ``/audio/volume/*`` family exists.
2. **Mute** — V2 devices take a value (``{"MUTE": bool}``) in one round
   trip instead of the read-then-toggle dance.
3. **max_volume** — read from the static settings tree rather than
   trusted from the hardcoded profile constant.

Background: home-assistant/core#179254 and ``docs/android-app-findings.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from vizaio import DeviceType, Vizio
from vizaio.wire import Response

from ._fixtures import (
    AUTH_TOKEN,
    SOUNDBAR_HOST_PORT,
    TV_HOST_PORT,
    make_item,
    make_success_response,
)

CAPTURED = Path(__file__).parent / "captured"


def _resp(payload: dict[str, Any]) -> Response:
    return Response.from_json(payload)


def _captured(name: str) -> Response:
    return Response.from_json(json.loads((CAPTURED / name).read_text()))


def _deviceinfo_with_api_version(version: str | None) -> Response:
    """The real capture, with ``API_VERSION`` swapped (or removed)."""
    raw = json.loads((CAPTURED / "device_info.json").read_text())
    value = raw["ITEMS"][0]["VALUE"]
    if version is None:
        value.pop("API_VERSION", None)
    else:
        value["API_VERSION"] = version
    return Response.from_json(raw)


@pytest.fixture
def vizio_tv() -> Vizio:
    return Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV, auth_token=AUTH_TOKEN)


@pytest.fixture
def vizio_soundbar() -> Vizio:
    return Vizio(host=SOUNDBAR_HOST_PORT, device_type=DeviceType.SOUNDBAR)


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr("vizaio.client.SmartCastClient.request_spec", mock)
    return mock


def _paths(mock: AsyncMock) -> list[tuple[str, ...]]:
    return [c.args[0].paths for c in mock.call_args_list]


def _suffixes(mock: AsyncMock) -> list[str]:
    return [c.kwargs.get("path_suffix", "") for c in mock.call_args_list]


class TestApiVersion:
    """``API_VERSION`` comes off the deviceinfo payload we already cache."""

    async def test_reads_api_version(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _captured("device_info.json")
        assert await vizio_tv.get_api_version() == "3.3.3-2538.0001"

    async def test_missing_api_version_is_empty_string(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _deviceinfo_with_api_version(None)
        assert await vizio_tv.get_api_version() == ""

    async def test_deviceinfo_is_fetched_once(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Repeated capability checks must not re-poll the device."""
        mock_client.return_value = _captured("device_info.json")
        await vizio_tv.get_api_version()
        await vizio_tv.get_api_version()
        await vizio_tv.supports_volume_v2()
        assert len(mock_client.call_args_list) == 1


class TestVolumeV2Detection:
    async def test_captured_tv_supports_v2(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """The basement TV's 3.3.3-2538.0001 clears the threshold."""
        mock_client.return_value = _captured("device_info.json")
        assert await vizio_tv.supports_volume_v2() is True

    async def test_issue_179254_tv_supports_v2(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _deviceinfo_with_api_version("3.7.2-2621.0005")
        assert await vizio_tv.supports_volume_v2() is True

    async def test_legacy_firmware_does_not(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _deviceinfo_with_api_version("1.0.13.25")
        assert await vizio_tv.supports_volume_v2() is False

    async def test_soundbar_never_supports_v2(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        """Audio-root devices are excluded regardless of API version."""
        mock_client.return_value = _deviceinfo_with_api_version("3.7.2-2621.0005")
        assert await vizio_soundbar.supports_volume_v2() is False

    async def test_unreachable_deviceinfo_degrades_to_false(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """A probe failure must not make the capability look available —
        we'd send the device endpoints it may not implement."""
        from vizaio import VizioConnectionError

        mock_client.side_effect = VizioConnectionError("unreachable")
        assert await vizio_tv.supports_volume_v2() is False


class TestMuteOnVolumeV2:
    """On V2 firmware, mute sets a value instead of toggling.

    Hardware (VHD24M-0810, panel on): ``PUT /audio/volume/mute``
    ``{"MUTE": bool}`` works, is idempotent under repeat, and is reflected
    by both the flat read and the ``menu_native`` leaf. That makes it
    strictly better than read-then-toggle — one round trip instead of
    three, and no race against someone pressing mute on the physical
    remote between our read and our write.

    Same shape as ``set_volume``: gated, no caller flag. Contrast
    ``volume_up``/``volume_down``, where the keypress path is already a
    single request so the V2 endpoint wins nothing and stays opt-in.
    """

    async def test_v2_mute_sets_the_value(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("device_info.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.mute()
        assert _paths(mock_client)[-1] == ("/audio/volume/mute",)
        assert mock_client.call_args.kwargs["body"] == {"MUTE": True}

    async def test_v2_unmute_sets_false(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("device_info.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.unmute()
        assert mock_client.call_args.kwargs["body"] == {"MUTE": False}

    async def test_v2_mute_never_reads_or_toggles(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """The read-then-toggle race is the thing we are removing."""
        mock_client.side_effect = [
            _captured("device_info.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.mute()
        assert ("/key_command/",) not in _paths(mock_client)
        assert len(mock_client.call_args_list) == 2

    async def test_legacy_firmware_still_toggles(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _deviceinfo_with_api_version("1.0.13.25"),
            _resp(make_success_response(items=[make_item("mute", "Off")])),
            _resp(make_success_response(items=[make_item("mute", "Off")])),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.mute()
        assert ("/key_command/",) in _paths(mock_client)

    async def test_soundbar_still_toggles_without_probing(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _resp(make_success_response(items=[make_item("mute", "Off")])),
            _resp(make_success_response(items=[make_item("mute", "Off")])),
            _resp(make_success_response(items=[])),
        ]
        await vizio_soundbar.mute()
        assert ("/state/device/deviceinfo",) not in _paths(mock_client)
        assert ("/key_command/",) in _paths(mock_client)


class TestRelativeVolumeOnV2:
    """``{"STEP": n}`` in the **body** — not the ``?STEP=n`` query the APK
    shows.

    Hardware (VHD24M-0810): the query form returns SUCCESS and moves the
    volume by exactly 1 regardless of the value; the body form applies the
    requested delta. Verified for both directions at several values, plus
    empty-body and no-body defaulting to 1.
    """

    async def test_volume_up_sends_step_in_the_body(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("device_info.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.volume_up(3, use_v2=True)
        assert _paths(mock_client)[-1] == ("/audio/volume/increase",)
        assert mock_client.call_args.kwargs["body"] == {"STEP": 3}

    async def test_volume_down_sends_step_in_the_body(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("device_info.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.volume_down(2, use_v2=True)
        assert _paths(mock_client)[-1] == ("/audio/volume/decrease",)
        assert mock_client.call_args.kwargs["body"] == {"STEP": 2}

    async def test_no_step_query_is_appended(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """The query form is a silent no-op on real firmware, so it must
        never be sent — a wrong-but-SUCCESS request is worse than none."""
        mock_client.side_effect = [
            _captured("device_info.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.volume_up(3, use_v2=True)
        assert _suffixes(mock_client) == ["", ""]

    async def test_keypress_remains_the_default(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """The official app tries the key command first on every device,
        vizaio already batches N keys into a single PUT, and app 5.3.0
        dropped the HTTP path entirely — so the default is unchanged."""
        mock_client.return_value = _resp(make_success_response(items=[]))
        await vizio_tv.volume_up(3)
        assert _paths(mock_client) == [("/key_command/",)]


class TestMaxVolume:
    """``max_volume`` is read from the static tree, profile is the fallback."""

    async def test_reads_maximum_from_static_tree(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
        ]
        assert await vizio_tv.get_max_volume() == 100

    async def test_falls_back_to_profile_when_static_unavailable(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        from vizaio import VizioNotFoundError

        mock_client.side_effect = VizioNotFoundError("no such uri")
        assert await vizio_soundbar.get_max_volume() == 31

    async def test_result_is_cached(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
        ]
        await vizio_tv.get_max_volume()
        calls = len(mock_client.call_args_list)
        await vizio_tv.get_max_volume()
        assert len(mock_client.call_args_list) == calls


class TestCapabilityProbeCost:
    """The probe must not cost a round trip when the profile already
    settles the question."""

    async def test_audio_device_skips_the_deviceinfo_fetch(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        """``isTvDevice()`` is knowable from the profile alone, so an
        audio-root device should answer without touching the network."""
        assert await vizio_soundbar.supports_volume_v2() is False
        assert mock_client.call_count == 0

    async def test_audio_device_mute_makes_no_extra_request(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(
            make_success_response(items=[make_item("mute", "Off")])
        )
        await vizio_soundbar.mute()
        assert ("/state/device/deviceinfo",) not in _paths(mock_client)


class TestSetVolumeGating:
    """``set_volume`` must respect the same gate as the rest of the family.

    ``PUT /audio/volume/level`` is V2-family — Vizio's own builder names it
    ``setVolumeLevelCommandV2`` — and the app only ever adds an HTTP volume
    rung for TVs. It also has **zero callers** in 5.0.0 and is absent from
    5.3.0, so it is unexercised by the vendor; sending it to a soundbar or
    to pre-V2 TV firmware is a guess. The fallback is the app's own V1
    branch: GET the leaf for its HASHVAL, then MODIFY.
    """

    async def test_v2_tv_uses_the_flat_put(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("device_info.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.set_volume(12)
        assert _paths(mock_client)[-1] == ("/audio/volume/level",)
        assert mock_client.call_args.kwargs["body"] == {"LEVEL": 12}

    async def test_legacy_tv_falls_back_to_hashval_write(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _deviceinfo_with_api_version("1.0.13.25"),
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.set_volume(12)
        assert ("/audio/volume/level",) not in _paths(mock_client)
        body = mock_client.call_args.kwargs["body"]
        assert body["REQUEST"] == "MODIFY"
        assert body["VALUE"] == 12
        assert body["HASHVAL"] == 3662031975

    async def test_soundbar_never_uses_the_flat_put(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        """The app adds an HTTP volume rung only for TVs; audio devices get
        key command + Cast. So the flat endpoint is pure speculation here."""
        mock_client.side_effect = [
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_soundbar.set_volume(12)
        assert ("/audio/volume/level",) not in _paths(mock_client)

    async def test_range_check_precedes_any_request(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Validation stays offline — no capability probe for a value that
        can never be sent."""
        from vizaio import VizioInvalidInputError

        with pytest.raises(VizioInvalidInputError, match="0-100"):
            await vizio_tv.set_volume(101)
        mock_client.assert_not_called()
