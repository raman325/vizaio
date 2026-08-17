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

    ``PUT /audio/volume/mute`` ``{"MUTE": bool}`` is idempotent under
    repeat, making it strictly better than read-then-toggle: one round
    trip instead of three, and no race against someone pressing mute on
    the physical remote between the read and the write.

    Gated, with no caller flag — same shape as ``set_volume``.
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


class TestRelativeVolumeAlwaysUsesKeypresses:
    """Relative volume has no caller knob — the library picks.

    The V2 ``increase``/``decrease`` endpoints work, but never beat the
    keypress path: a keypress batch is already a single PUT for any step
    count up to ``_KEYLIST_CHUNK_SIZE`` (50), which covers every
    realistic input on a 0-100 scale, and it works on every device
    family regardless of firmware.

    Exposing a flag would push a choice onto the caller that the library
    can already answer — and whose "on" position is never the better one.
    """

    async def test_v2_firmware_still_uses_keypresses(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(make_success_response(items=[]))
        await vizio_tv.volume_up(3)
        assert _paths(mock_client) == [("/key_command/",)]

    async def test_no_capability_probe_is_issued(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """No deviceinfo fetch either — the answer can't change the path."""
        mock_client.return_value = _resp(make_success_response(items=[]))
        await vizio_tv.volume_down(2)
        assert ("/state/device/deviceinfo",) not in _paths(mock_client)

    async def test_batches_into_one_request(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """The reason V2 wins nothing: N steps is still one PUT."""
        mock_client.return_value = _resp(make_success_response(items=[]))
        await vizio_tv.volume_up(10)
        assert len(mock_client.call_args_list) == 1
        assert len(mock_client.call_args.kwargs["body"]["KEYLIST"]) == 10

    async def test_no_use_v2_parameter_exists(self) -> None:
        import inspect

        for method in (Vizio.volume_up, Vizio.volume_down):
            assert "use_v2" not in inspect.signature(method).parameters


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

    ``PUT /audio/volume/level`` is V2-family and TV-only, so sending it
    to a soundbar or to pre-V2 firmware is a guess. The fallback is GET
    the leaf for its HASHVAL, then MODIFY.
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


class TestV2FallbackOnUnsupportedEndpoint:
    """A device that clears the version gate but lacks the endpoint must
    still work.

    A qualifying ``API_VERSION`` does not guarantee the endpoint exists.
    If a TV reports one but answers ``URI_NOT_FOUND`` on the flat leaf,
    the V2 path must degrade to the path that works everywhere rather
    than hard-failing.
    """

    async def test_mute_falls_back_when_endpoint_missing(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        from vizaio import VizioNotFoundError

        mock_client.side_effect = [
            _captured("device_info.json"),
            VizioNotFoundError("no such uri"),  # flat mute PUT absent
            # Fallback is read-then-toggle. The read is gated the same
            # way, so on a device missing the flat family it also probes
            # and falls through to the leaf.
            VizioNotFoundError("no such uri"),  # flat mute GET absent
            _resp(make_success_response(items=[make_item("mute", "Off")])),
            _resp(make_success_response(items=[make_item("mute", "Off")])),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.mute()
        assert ("/key_command/",) in _paths(mock_client)

    async def test_set_volume_falls_back_when_endpoint_missing(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        from vizaio import VizioNotFoundError

        mock_client.side_effect = [
            _captured("device_info.json"),
            VizioNotFoundError("no such uri"),
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.set_volume(12)
        body = mock_client.call_args.kwargs["body"]
        assert body["REQUEST"] == "MODIFY"
        assert body["VALUE"] == 12

    async def test_other_errors_still_propagate(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Only 'this endpoint does not exist here' triggers the fallback.
        A transient failure must not be silently retried down another path."""
        from vizaio import VizioConnectionError

        mock_client.side_effect = [
            _captured("device_info.json"),
            VizioConnectionError("boom"),
        ]
        with pytest.raises(VizioConnectionError):
            await vizio_tv.mute()


class TestRelativeVolumeGuards:
    """Non-positive steps are a no-op, as they always were."""

    @pytest.mark.parametrize("steps", [0, -3])
    async def test_non_positive_steps_send_nothing(
        self, vizio_tv: Vizio, mock_client: AsyncMock, steps: int
    ) -> None:
        await vizio_tv.volume_up(steps)
        mock_client.assert_not_called()


class TestMaxVolumeFailureIsNotCached:
    async def test_failed_lookup_retries_next_call(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Falling back to the profile constant is a degraded answer, not
        an answer — pinning it for the session means a TV that was asleep
        on first call never reports its real scale."""
        from vizaio import VizioConnectionError

        mock_client.side_effect = [
            VizioConnectionError("asleep"),
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
        ]
        assert await vizio_tv.get_max_volume() == 100  # profile fallback
        after_failure = len(mock_client.call_args_list)
        assert await vizio_tv.get_max_volume() == 100  # same number, but...
        # ...it must have gone back to the device rather than reusing the
        # degraded answer. (TV_PROFILE.max_volume and the device's MAXIMUM
        # are both 100, so only the call count distinguishes the two.)
        assert len(mock_client.call_args_list) > after_failure

    async def test_successful_lookup_is_cached(
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


class TestCapabilityProbeRecoversFromTransientFailure:
    """A capability answer must be cached when *known*, retried when not.

    ``_get_deviceinfo`` negative-caches and re-raises for the session, so
    a V2 TV that was asleep or briefly unreachable on the very first
    probe would otherwise be pinned to the legacy paths for the entire
    life of the ``Vizio`` object — in Home Assistant, the life of the
    config entry — silently losing the race-free mute this gate exists to
    deliver. ``_resolve_max_volume`` already refuses to cache a failure
    for exactly this reason; the capability probe must match.
    """

    async def test_transient_failure_is_retried(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        from vizaio import VizioConnectionError

        mock_client.side_effect = [
            VizioConnectionError("asleep"),
            _captured("device_info.json"),
        ]
        assert await vizio_tv.supports_volume_v2() is False  # degraded
        assert await vizio_tv.supports_volume_v2() is True  # recovered

    async def test_known_answer_is_cached(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _captured("device_info.json")
        assert await vizio_tv.supports_volume_v2() is True
        calls = len(mock_client.call_args_list)
        assert await vizio_tv.supports_volume_v2() is True
        assert len(mock_client.call_args_list) == calls


class TestReadsUseTheSameSurfaceAsWrites:
    """Reads follow the gate too, so a V2 device isn't written on one
    surface and read from another.

    Also halves the poll cost: the menu_native leaf reads are two GETs
    each (value + static options), the flat reads are one and carry no
    HASHVAL.
    """

    async def test_v2_volume_read_uses_the_flat_endpoint(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("device_info.json"),
            _captured("audio_volume_level.json"),
        ]
        assert await vizio_tv.get_volume() == 9
        assert _paths(mock_client)[-1] == ("/audio/volume/level",)

    async def test_v2_mute_read_uses_the_flat_endpoint(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("device_info.json"),
            _captured("audio_volume_mute.json"),
        ]
        assert await vizio_tv.is_muted() is False
        assert _paths(mock_client)[-1] == ("/audio/volume/mute",)

    async def test_v2_read_is_one_request(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """The menu_native path costs two (value + static options)."""
        mock_client.side_effect = [
            _captured("device_info.json"),
            _captured("audio_volume_level.json"),
            _captured("audio_volume_level.json"),
        ]
        await vizio_tv.get_volume()
        before = len(mock_client.call_args_list)
        await vizio_tv.get_volume()
        assert len(mock_client.call_args_list) == before + 1

    async def test_falls_back_to_the_leaf_when_flat_is_missing(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        from vizaio import VizioNotFoundError

        mock_client.side_effect = [
            _captured("device_info.json"),
            VizioNotFoundError("no such uri"),
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
        ]
        assert await vizio_tv.get_volume() == 23
        assert _paths(mock_client)[-1] == ("/menu_native/static/tv_settings",)

    async def test_legacy_firmware_reads_the_leaf(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _deviceinfo_with_api_version("1.0.13.25"),
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
        ]
        assert await vizio_tv.get_volume() == 23
        assert ("/audio/volume/level",) not in _paths(mock_client)

    async def test_soundbar_reads_the_leaf_without_probing(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
        ]
        assert await vizio_soundbar.get_volume() == 23
        assert ("/state/device/deviceinfo",) not in _paths(mock_client)


class TestMaxVolumeAgreesWithSetVolume:
    """``get_max_volume()`` must never return a value ``set_volume()``
    rejects — otherwise the obvious ``set_volume(await get_max_volume())``
    raises, and reconciling the two numbers becomes the caller's job."""

    async def test_never_exceeds_the_write_bound(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        """Soundbar static tree says 100; the profile clamps writes at 31
        (pyvizio #125). The accessor must report the bound that writes
        actually honour."""
        mock_client.side_effect = [
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
        ]
        assert await vizio_soundbar.get_max_volume() == 31

    async def test_reported_max_is_always_settable(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _captured("settings_audio_volume.json"),
            _captured("static_audio.json"),
            _captured("device_info.json"),
            _resp(make_success_response(items=[])),
        ]
        await vizio_tv.set_volume(await vizio_tv.get_max_volume())


class TestMaxVolumeCachesAnAnsweredNoBound:
    async def test_device_without_static_bound_is_not_reprobed(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """ "Device answered, has no MAXIMUM" is a real answer and must
        cache; only "device didn't answer" should retry. Otherwise this
        device class pays two GETs on every single call, forever."""
        no_bound = _resp(make_success_response(items=[make_item("volume", 9)]))
        mock_client.side_effect = [no_bound, no_bound]
        assert await vizio_tv.get_max_volume() == 100  # profile fallback
        calls = len(mock_client.call_args_list)
        assert await vizio_tv.get_max_volume() == 100
        assert len(mock_client.call_args_list) == calls
