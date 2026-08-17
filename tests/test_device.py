"""Vizio class behavior tests.

Strategy: mock ``SmartCastClient.request_spec`` to return canned ``Response``
objects built from fixtures. Each test asserts a single observable
behavior of one ``Vizio`` method. No HTTP, no aiohttp, no transport.

Naming: tests are grouped by feature (Power, Volume, Input, ...) matching
the public API surface. Each group is the equivalent test from
``pyvizio/tests/test_async_api.py`` translated to the new exception-raising
contract — an excellent migration source for callers updating from pyvizio.

The HA migration cheatsheet (``assets/ha_migration_cheatsheet.md``) is
derived directly from this file: each test class corresponds to one
migration row.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests._fixtures import (
    AUTH_TOKEN,
    CRAVE_HOST_PORT,
    DEFAULT_AUDIO_OPTIONS,
    DEFAULT_AUDIO_SETTINGS,
    DEFAULT_TV_INPUTS,
    SOUNDBAR_HOST_PORT,
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
    make_settings_options_response,
    make_settings_response,
    make_success_response,
)
from vizaio import (
    AppConfig,
    ChargingStatus,
    DeviceType,
    InputInfo,
    PairChallenge,
    RemoteKey,
    SettingInfo,
    Vizio,
    VizioAuthError,
    VizioConnectionError,
    VizioInvalidInputError,
    VizioInvalidParameterError,
    VizioResponseError,
    VizioUnsupportedError,
)
from vizaio.wire import Response

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vizio_tv() -> Vizio:
    """A TV-typed Vizio instance with a fixed host and auth token."""
    return Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV, auth_token=AUTH_TOKEN)


@pytest.fixture
def vizio_soundbar() -> Vizio:
    return Vizio(host=SOUNDBAR_HOST_PORT, device_type=DeviceType.SOUNDBAR)


@pytest.fixture
def vizio_crave() -> Vizio:
    return Vizio(host=CRAVE_HOST_PORT, device_type=DeviceType.CRAVE360)


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace SmartCastClient.request_spec with an AsyncMock.

    Tests configure the mock per-call to return canned Response objects.
    Returning a list of Response objects makes the mock cycle through
    them, allowing tests of multi-request flows (GET-then-PUT, retry
    paths) without complex side_effect setup.
    """
    mock = AsyncMock()
    monkeypatch.setattr("vizaio.client.SmartCastClient.request_spec", mock)
    return mock


def _resp(payload: dict) -> Response:
    """Convenience: turn a fixture dict into a parsed Response."""
    return Response.from_json(payload)


def _last_call_paths(mock: AsyncMock) -> tuple[str, ...]:
    """Inspect which paths the most recent EndpointSpec covered."""
    args, _ = mock.call_args
    spec = args[0]
    return spec.paths  # type: ignore[no-any-return]


def _all_call_paths(mock: AsyncMock) -> list[tuple[str, ...]]:
    """Path tuples from every call, in order."""
    return [c.args[0].paths for c in mock.call_args_list]


def _v2_deviceinfo() -> Response:
    """A deviceinfo payload whose API version clears the volume-V2 gate.

    ``set_volume`` probes :meth:`Vizio.supports_volume_v2` before choosing
    between the flat PUT and the HASHVAL write. Tests that mean to
    exercise the flat path feed this first.
    """
    return Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {"CNAME": "deviceinfo", "VALUE": {"API_VERSION": "3.3.3-2538.0001"}}
            ],
        }
    )


def _legacy_deviceinfo() -> Response:
    """A deviceinfo payload whose API version predates volume-V2.

    ``mute()``/``unmute()``/``set_volume()`` probe
    :meth:`Vizio.supports_volume_v2` before choosing a strategy. Tests
    that mean to exercise the read-then-toggle / HASHVAL fallbacks feed
    this first so the probe resolves to the legacy branch.
    """
    return Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [{"CNAME": "deviceinfo", "VALUE": {"API_VERSION": "1.0.13.25"}}],
        }
    )


def _last_call_body(mock: AsyncMock) -> dict:
    """Inspect the PUT body of the most recent request.

    Asserts the body is present — tests that call this expect a PUT.
    """
    body = mock.call_args.kwargs.get("body")
    assert body is not None, "expected a body on the last request"
    return body


# ===========================================================================
# Construction
# ===========================================================================


class TestConstruction:
    """Migration: VizioAsync(device_id, host, name, auth, device_type) →
    Vizio(host, *, device_type=, auth_token=).

    device_id and name are gone from the constructor — they're pairing-time
    metadata, passed to pair_session() instead.
    """

    @pytest.mark.parametrize("dtype", list(DeviceType))
    def test_valid_device_types(self, dtype: DeviceType) -> None:
        v = Vizio(host="1.2.3.4:7345", device_type=dtype)
        assert v.profile is dtype.profile

    def test_default_device_type_is_tv(self) -> None:
        v = Vizio(host="1.2.3.4:7345")
        assert v.profile is DeviceType.TV.profile

    def test_host_with_port(self) -> None:
        v = Vizio(host="1.2.3.4:7345")
        assert v.host == "1.2.3.4:7345"

    def test_host_without_port_will_resolve(self) -> None:
        """Constructor accepts host without port; port resolves on first
        request via DEFAULT_PORTS probe."""
        v = Vizio(host="1.2.3.4")
        # Specific resolution is async — just verify construction works.
        assert v.host == "1.2.3.4"

    def test_custom_profile(self) -> None:
        from vizaio import TV_PROFILE

        v = Vizio(host="1.2.3.4", profile=TV_PROFILE)
        assert v.profile is TV_PROFILE


# ===========================================================================
# Lifecycle (context manager, ping, close)
# ===========================================================================


class TestLifecycle:
    """Migration: was no equivalent in pyvizio.

    `Vizio` is now an async context manager that owns its aiohttp session.
    Ping is a cheap connection probe.
    """

    async def test_async_context_manager(self, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(make_device_info_response({}))
        async with Vizio(host="1.2.3.4:7345") as v:
            await v.ping()
        # Session closed on __aexit__ (verified by no-leaked-session via
        # warning-as-error in pytest config).

    async def test_ping_uses_unauthenticated_endpoint(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(make_device_info_response({}))
        await vizio_tv.ping()
        assert _last_call_paths(mock_client) == ("/state/device/deviceinfo",)

    async def test_ping_raises_on_connection_error(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = VizioConnectionError("device unreachable")
        with pytest.raises(VizioConnectionError):
            await vizio_tv.ping()


# ===========================================================================
# Power
# ===========================================================================


class TestPower:
    """Migration:
    - get_power_state() -> bool (was bool|None, raised on error vs returning None)
    - power_on() -> None (was pow_on() -> bool|None)
    - power_off() -> None (was pow_off() -> bool|None)
    - power_toggle() -> None (renamed from pyvizio's pow_toggle())
    """

    @pytest.mark.parametrize("value,expected", [(1, True), (0, False)])
    async def test_get_power_state(
        self,
        vizio_tv: Vizio,
        mock_client: AsyncMock,
        value: int,
        expected: bool,
    ) -> None:
        mock_client.return_value = _resp(make_power_response(value))
        assert await vizio_tv.get_power_state() is expected

    async def test_get_power_state_propagates_error(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = VizioConnectionError("unreachable")
        with pytest.raises(VizioConnectionError):
            await vizio_tv.get_power_state()

    async def test_power_on(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.power_on()
        assert _last_call_paths(mock_client) == ("/key_command/",)
        body = _last_call_body(mock_client)
        # POW_ON is codeset 11, code 1.
        assert body == {"KEYLIST": [{"CODESET": 11, "CODE": 1, "ACTION": "KEYPRESS"}]}

    async def test_power_off(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.power_off()
        body = _last_call_body(mock_client)
        # Regression test for pyvizio open issue #163 ("Power On command
        # turns power Off"): POW_OFF must be code 0, not code 1.
        assert body == {"KEYLIST": [{"CODESET": 11, "CODE": 0, "ACTION": "KEYPRESS"}]}

    async def test_power_toggle(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        # power_toggle sends POW_TOGGLE (codeset 11, code 2) in a single
        # round trip — distinct from power_on/off, which always send the
        # same key regardless of state.
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.power_toggle()
        assert _last_call_paths(mock_client) == ("/key_command/",)
        body = _last_call_body(mock_client)
        assert body == {"KEYLIST": [{"CODESET": 11, "CODE": 2, "ACTION": "KEYPRESS"}]}
        # And exactly one call — no state query first.
        assert mock_client.await_count == 1


# ===========================================================================
# Volume / mute
# ===========================================================================


class TestVolume:
    """Migration:
    - get_current_volume() -> int (was int|None) — renamed to get_volume()
    - vol_up(num=N) -> None — renamed to volume_up(steps=N)
    - vol_down(num=N) -> None — renamed to volume_down(steps=N)
    """

    async def test_get_volume(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(
            make_success_response(
                items=[make_item("volume", 25, item_type="T_VALUE_ABS_V1")]
            )
        )
        assert await vizio_tv.get_volume() == 25

    async def test_set_volume_flat_no_hashval(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """On volume-V2 firmware: flat /audio/volume/level PUT, body
        {LEVEL:n}, no HASHVAL GET. One deviceinfo GET resolves the gate
        (cached for the session); see TestSetVolumeGating in
        test_device_volume_v2.py for the non-V2 fallback."""
        mock_client.side_effect = [
            _v2_deviceinfo(),
            _resp(make_success_response()),
        ]
        await vizio_tv.set_volume(12)
        assert _last_call_paths(mock_client) == ("/audio/volume/level",)
        assert _last_call_body(mock_client) == {"LEVEL": 12}

    async def test_set_volume_out_of_range_raises(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(VizioInvalidInputError, match="0-100"):
            await vizio_tv.set_volume(101)
        mock_client.assert_not_called()

    async def test_set_volume_soundbar_range(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        """Range is the profile's max_volume (soundbar = 31)."""
        with pytest.raises(VizioInvalidInputError, match="0-31"):
            await vizio_soundbar.set_volume(50)

    async def test_volume_up(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.volume_up()
        body = _last_call_body(mock_client)
        # VOL_UP is codeset 5, code 1.
        assert body["KEYLIST"] == [{"CODESET": 5, "CODE": 1, "ACTION": "KEYPRESS"}]

    async def test_volume_up_steps(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.volume_up(steps=3)
        body = _last_call_body(mock_client)
        assert len(body["KEYLIST"]) == 3
        for entry in body["KEYLIST"]:
            assert entry == {"CODESET": 5, "CODE": 1, "ACTION": "KEYPRESS"}

    async def test_volume_up_large_steps_single_put(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Per APK findings, the official app does NOT chunk KEYLISTs —
        it sends arbitrary-length lists in one PUT. We match that
        behavior up to a defensive cap of 50 to handle worst-case device
        buffers (see protocol-notes #19)."""
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.volume_up(steps=30)
        # Single PUT, 30-element KEYLIST.
        assert mock_client.call_count == 1
        body = _last_call_body(mock_client)
        assert len(body["KEYLIST"]) == 30

    async def test_volume_up_chunks_only_at_defensive_cap(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Above the defensive cap (50), chunk into multiple PUTs."""
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.volume_up(steps=120)
        # 120 / 50 = 3 PUTs (50+50+20).
        assert mock_client.call_count == 3

    async def test_volume_down(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.volume_down()
        body = _last_call_body(mock_client)
        assert body["KEYLIST"] == [{"CODESET": 5, "CODE": 0, "ACTION": "KEYPRESS"}]


class TestMute:
    """Migration:
    - is_muted() -> bool (was bool|None)
    - mute_on() -> None — renamed to mute()
    - mute_off() -> None — renamed to unmute()
    - mute_toggle() removed — caller composes from is_muted + mute/unmute
    """

    @pytest.mark.parametrize("value,expected", [("On", True), ("Off", False)])
    async def test_is_muted(
        self,
        vizio_tv: Vizio,
        mock_client: AsyncMock,
        value: str,
        expected: bool,
    ) -> None:
        mock_client.return_value = _resp(
            make_success_response(items=[make_item("mute", value)])
        )
        assert await vizio_tv.is_muted() is expected

    async def test_mute_when_unmuted_sends_toggle(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        ``mute()`` is state-aware: read mute setting, send MUTE_TOGGLE
        only if currently unmuted. Discrete MUTE_ON / MUTE_OFF codes
        are firmware-class-specific (verified on VHD24M-0810 fw
        3.720.9.1-1: discrete codes don't exist as discrete actions —
        they all behave as toggles).

        One deviceinfo GET resolves the volume-V2 probe to the legacy
        branch, ``is_muted`` uses ``get_setting`` which fires two GETs
        (dynamic value + static options), then the toggle is one PUT.
        """
        mock_client.side_effect = [
            _legacy_deviceinfo(),
            _resp(make_success_response(items=[make_item("mute", "Off")])),
            _resp(make_success_response(items=[make_item("mute", "Off")])),
            _resp(make_key_press_response()),
        ]
        await vizio_tv.mute()
        # MUTE_TOGGLE is codeset 5, code 4.
        body = _last_call_body(mock_client)
        assert body["KEYLIST"][0]["CODESET"] == 5
        assert body["KEYLIST"][0]["CODE"] == 4
        assert mock_client.call_count == 4

    async def test_mute_when_muted_is_noop(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Already muted — no toggle sent (idempotent)."""
        mock_client.side_effect = [
            _legacy_deviceinfo(),
            _resp(make_success_response(items=[make_item("mute", "On")])),
            _resp(make_success_response(items=[make_item("mute", "On")])),
        ]
        await vizio_tv.mute()
        # deviceinfo probe + two GETs for is_muted (dynamic + static
        # options), no PUT.
        assert mock_client.call_count == 3

    async def test_unmute_when_muted_sends_toggle(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _legacy_deviceinfo(),
            _resp(make_success_response(items=[make_item("mute", "On")])),
            _resp(make_success_response(items=[make_item("mute", "On")])),
            _resp(make_key_press_response()),
        ]
        await vizio_tv.unmute()
        body = _last_call_body(mock_client)
        assert body["KEYLIST"][0]["CODESET"] == 5
        assert body["KEYLIST"][0]["CODE"] == 4
        assert mock_client.call_count == 4

    async def test_unmute_when_unmuted_is_noop(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _legacy_deviceinfo(),
            _resp(make_success_response(items=[make_item("mute", "Off")])),
            _resp(make_success_response(items=[make_item("mute", "Off")])),
        ]
        await vizio_tv.unmute()
        assert mock_client.call_count == 3

    async def test_mute_toggle_sends_key_without_state_query(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        # mute_toggle sends MUTE_TOGGLE (codeset 5, code 4) in one round
        # trip — half the cost of mute()/unmute(), which read is_muted()
        # first to be idempotent. Caller doesn't learn the resulting state.
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.mute_toggle()
        assert _last_call_paths(mock_client) == ("/key_command/",)
        body = _last_call_body(mock_client)
        assert body == {"KEYLIST": [{"CODESET": 5, "CODE": 4, "ACTION": "KEYPRESS"}]}
        assert mock_client.await_count == 1


# ===========================================================================
# Inputs
# ===========================================================================


class TestInputs:
    """Migration:
    - get_inputs_list() -> list[InputItem] | None → get_inputs() -> list[InputInfo]
    - get_current_input() -> str | None → get_current_input() -> str (raises)
    - set_input(name) -> bool | None → set_input(name) -> None (raises)
    - next_input() -> bool | None → next_input() -> None (raises)
    """

    async def test_get_inputs(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(
            make_inputs_list_response(
                DEFAULT_TV_INPUTS, current_input_meta_name="Living Room TV"
            )
        )
        inputs = await vizio_tv.get_inputs()
        assert len(inputs) == 5
        assert all(isinstance(i, InputInfo) for i in inputs)
        # Synthetic 'current_input' item is filtered out.
        assert "current_input" not in {i.name for i in inputs}

    async def test_get_inputs_marks_current(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(
            make_inputs_list_response(DEFAULT_TV_INPUTS, current_input_meta_name="PS5")
        )
        inputs = await vizio_tv.get_inputs()
        # The synthetic current_input had meta="PS5"; map back to HDMI-2.
        current = [i for i in inputs if i.is_current]
        assert len(current) == 1
        assert current[0].meta_name == "PS5"

    async def test_get_inputs_works_without_synthetic(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Quirk #6 hardware-verify: parser must work even if firmware
        doesn't include the synthetic current_input item."""
        mock_client.return_value = _resp(
            make_inputs_list_response(
                DEFAULT_TV_INPUTS, include_synthetic_current=False
            )
        )
        inputs = await vizio_tv.get_inputs()
        # All inputs have is_current=False since we couldn't determine.
        assert len(inputs) == 5
        assert all(not i.is_current for i in inputs)

    async def test_get_current_input(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(
            make_current_input_response("current_input", "HDMI-1", 5)
        )
        assert await vizio_tv.get_current_input() == "HDMI-1"

    async def test_set_input(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        # set_input does GET (current input + inputs list for validation)
        # then PUT (the modify).
        mock_client.side_effect = [
            _resp(
                make_inputs_list_response(
                    DEFAULT_TV_INPUTS,
                    current_input_meta_name="Living Room TV",
                )
            ),
            _resp(make_current_input_response("current_input", "Living Room TV", 5)),
            _resp(make_success_response()),
        ]
        await vizio_tv.set_input("HDMI-2")

    async def test_set_input_invalid_name_raises(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """`set_input` validates against the current input list. Raises
        VizioInvalidInputError with the valid names listed."""
        mock_client.return_value = _resp(
            make_inputs_list_response(
                DEFAULT_TV_INPUTS, current_input_meta_name="HDMI-1"
            )
        )
        with pytest.raises(VizioInvalidInputError, match="HDMI-99"):
            await vizio_tv.set_input("HDMI-99")
        # Validation happens after the inputs+current_input GETs but
        # before the PUT — exactly two HTTP calls fired.
        assert mock_client.call_count == 2

    async def test_set_input_uses_put_not_get(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        Regression: set_input previously called request_spec with the
        Endpoint.CURRENT_INPUT spec verbatim — but that spec's method is
        GET (read-side dominates). The device returns success on a GET
        with a body but ignores the body, so set_input silently no-op'd
        on real hardware. Verified live against VHD24M-0810. Fix:
        replace(spec, method='PUT') matching the _put_setting pattern.
        """
        mock_client.side_effect = [
            _resp(
                make_inputs_list_response(
                    DEFAULT_TV_INPUTS, current_input_meta_name="HDMI-1"
                )
            ),
            _resp(make_current_input_response("current_input", "HDMI-1", 5)),
            _resp(make_success_response()),
        ]
        await vizio_tv.set_input("HDMI-2")
        # The third call is the write — its spec must be PUT.
        write_call = mock_client.call_args_list[-1]
        spec = write_call.args[0]
        assert spec.method == "PUT", (
            f"set_input must issue PUT, got {spec.method!r} — "
            "the device silently ignores GET-with-body and returns success"
        )

    async def test_set_input_translates_name_to_cname(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        The PUT body must carry the input's **cname** (the device's
        canonical lowercase identifier), not the user's input string,
        and not the display name or meta_name. Verified live on
        VHD24M-0810: PUT VALUE='HDMI-2' → FAILURE; PUT VALUE='Mac' →
        HASHVAL_ERROR; PUT VALUE='hdmi2' → SUCCESS.
        """
        # Tuples are (cname, display_name, meta_name, hashval).
        diverging_inputs = [
            ("cast", "CAST", "SMARTCAST", 1),
            ("hdmi1", "HDMI-1", "PS5", 2),
        ]
        mock_client.side_effect = [
            _resp(make_inputs_list_response(diverging_inputs)),
            # current_input shows the device on PS5 — distinct from
            # target so the already-on-target short-circuit doesn't fire.
            _resp(make_current_input_response("current_input", "PS5", 99)),
            _resp(make_success_response()),
        ]
        # User passes the display name 'CAST' — must translate to cname 'cast'.
        await vizio_tv.set_input("CAST")
        body = _last_call_body(mock_client)
        assert body["VALUE"] == "cast", (
            f"set_input must translate to cname; got {body['VALUE']!r}"
        )

    async def test_set_input_accepts_meta_name_translates_to_cname(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """User passing the meta_name resolves to the input's cname."""
        diverging_inputs = [
            ("hdmi1", "HDMI-1", "PS5", 2),
            ("hdmi2", "HDMI-2", "Switch", 3),
        ]
        mock_client.side_effect = [
            _resp(make_inputs_list_response(diverging_inputs)),
            # Currently on Switch (cname=hdmi2); user wants PS5 (cname=hdmi1).
            _resp(make_current_input_response("current_input", "Switch", 7)),
            _resp(make_success_response()),
        ]
        await vizio_tv.set_input("PS5")
        body = _last_call_body(mock_client)
        assert body["VALUE"] == "hdmi1"

    async def test_set_input_accepts_cname_directly(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """User passing the cname directly (power-user form) passes through."""
        diverging_inputs = [
            ("cast", "CAST", "SMARTCAST", 1),
            ("hdmi1", "HDMI-1", "PS5", 2),
        ]
        mock_client.side_effect = [
            _resp(make_inputs_list_response(diverging_inputs)),
            _resp(make_current_input_response("current_input", "SMARTCAST", 5)),
            _resp(make_success_response()),
        ]
        await vizio_tv.set_input("hdmi1")
        body = _last_call_body(mock_client)
        assert body["VALUE"] == "hdmi1"

    async def test_set_input_short_circuits_when_already_on_target(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        Captured live from VHD24M-0810 fw 3.720.9.1-1: setting
        current_input to the value it already holds returns FAILURE.
        We short-circuit on match (no PUT issued, no exception raised)
        so callers get sensible no-op behavior.
        """
        inputs = [
            ("cast", "CAST", "SMARTCAST", 1),
            ("hdmi1", "HDMI-1", "HDMI-1", 2),
        ]
        mock_client.side_effect = [
            _resp(make_inputs_list_response(inputs)),
            _resp(make_current_input_response("current_input", "SMARTCAST", 99)),
        ]
        # 'CAST' resolves to meta_name 'SMARTCAST' which is already current.
        await vizio_tv.set_input("CAST")
        # Only the two GETs fired — no PUT.
        assert mock_client.call_count == 2

    async def test_get_inputs_marks_current_when_meta_diverges_from_name(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        Regression for the live VHD24M-0810 bug: device's
        ``current_input`` returns the meta_name (e.g., 'SMARTCAST'),
        which is NOT the cname-derived display name ('CAST'). Modern
        firmware doesn't include a synthetic current_input item inside
        the inputs response, so get_inputs must fetch current_input
        separately and pass the value to parse_inputs.
        """
        inputs = [
            ("cast", "CAST", "SMARTCAST", 1),
            ("hdmi1", "HDMI-1", "HDMI-1", 2),
        ]
        mock_client.side_effect = [
            # Inputs response with NO synthetic current_input entry.
            _resp(make_inputs_list_response(inputs, include_synthetic_current=False)),
            # Separate current_input fetch — meta_name 'SMARTCAST'.
            _resp(make_current_input_response("current_input", "SMARTCAST", 1)),
        ]
        result = await vizio_tv.get_inputs()
        cast_input = next(i for i in result if i.name == "CAST")
        assert cast_input.is_current, (
            "is_current must match by meta_name — "
            "device returns 'SMARTCAST' but our display name is 'CAST'"
        )

    async def test_next_input(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.next_input()
        # INPUT_NEXT is sent.
        body = _last_call_body(mock_client)
        # Check codeset is 7 (input keyset).
        assert body["KEYLIST"][0]["CODESET"] == 7

    async def test_inputs_unsupported_on_soundbar(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        """Profile gating: soundbars don't have inputs."""
        with pytest.raises(VizioUnsupportedError):
            await vizio_soundbar.get_inputs()
        # No HTTP call made.
        mock_client.assert_not_called()


# ===========================================================================
# Remote
# ===========================================================================


class TestRemote:
    """Migration:
    - remote(key) -> bool | None → send_key(key) -> None (raises on invalid)
    - get_remote_keys_list() -> KeysView[str] → available_keys property
    - play() / pause() / ch_up() / etc. removed — use send_key() directly
    """

    async def test_send_key_str(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.send_key("PLAY")
        body = _last_call_body(mock_client)
        # PLAY is codeset 2, code 3.
        assert body["KEYLIST"][0] == {
            "CODESET": 2,
            "CODE": 3,
            "ACTION": "KEYPRESS",
        }

    async def test_send_key_enum(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.send_key(RemoteKey.PAUSE)
        body = _last_call_body(mock_client)
        # PAUSE is codeset 2, code 2.
        assert body["KEYLIST"][0] == {
            "CODESET": 2,
            "CODE": 2,
            "ACTION": "KEYPRESS",
        }

    async def test_send_key_invalid_raises(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Migration: pyvizio's `remote("INVALID_KEY")` returned False
        silently. We raise `VizioUnsupportedError` so the bug is loud."""
        with pytest.raises(VizioUnsupportedError, match="INVALID_KEY"):
            await vizio_tv.send_key("INVALID_KEY")
        mock_client.assert_not_called()

    async def test_send_text_ascii_codes(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Each char -> codeset 0, code = ASCII code point, in one KEYLIST."""
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.send_text("Hi 5")
        assert mock_client.await_count == 1
        assert _last_call_paths(mock_client) == ("/key_command/",)
        body = _last_call_body(mock_client)
        assert body["KEYLIST"] == [
            {"CODESET": 0, "CODE": 72, "ACTION": "KEYPRESS"},  # H
            {"CODESET": 0, "CODE": 105, "ACTION": "KEYPRESS"},  # i
            {"CODESET": 0, "CODE": 32, "ACTION": "KEYPRESS"},  # space
            {"CODESET": 0, "CODE": 53, "ACTION": "KEYPRESS"},  # 5
        ]

    async def test_send_text_empty_is_noop(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        await vizio_tv.send_text("")
        mock_client.assert_not_called()

    async def test_send_text_non_ascii_raises(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(VizioInvalidInputError, match="ASCII"):
            await vizio_tv.send_text("café")
        mock_client.assert_not_called()

    async def test_send_text_chunks_above_cap(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Long strings split into multiple PUTs (defensive KEYLIST cap=50)."""
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.send_text("a" * 120)
        assert mock_client.await_count == 3  # 50 + 50 + 20
        lengths = [
            len(c.kwargs["body"]["KEYLIST"]) for c in mock_client.await_args_list
        ]
        assert lengths == [50, 50, 20]

    async def test_available_keys(self, vizio_tv: Vizio) -> None:
        keys = vizio_tv.available_keys
        assert "PLAY" in keys
        assert "PAUSE" in keys
        assert "VOL_UP" in keys
        assert "POW_ON" in keys
        # Property — no HTTP, no async.

    async def test_available_keys_differ_per_profile(
        self, vizio_tv: Vizio, vizio_soundbar: Vizio
    ) -> None:
        """Soundbars don't have channel keys."""
        assert "CH_UP" in vizio_tv.available_keys
        assert "CH_UP" not in vizio_soundbar.available_keys


# ===========================================================================
# Settings
# ===========================================================================


class TestSettings:
    """Migration:
    - get_setting_types_list() → get_setting_types()
    - get_all_settings(type) → get_settings(type) -> dict[str, SettingInfo]
    - get_setting(type, name) → returns SettingInfo (was raw value)
    - set_setting(type, name, value) → set_setting(..., *, hashval=None)
    - All audio convenience methods (get_audio_setting, etc.) removed —
      use get_setting('audio', name) directly.
    - get_setting_options/options_xlist removed — folded into SettingInfo.
    """

    async def test_get_setting_types(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(
            make_setting_types_response(
                ["audio", "picture", "system", "cast", "input", "network", "devices"]
            )
        )
        types = await vizio_tv.get_setting_types()
        # No filtering by default — protocol-notes #7 says we'll start
        # without filtering and adjust if hardware testing shows the
        # "menu" categories return malformed data when accessed.
        assert "audio" in types
        assert "picture" in types

    async def test_get_settings_returns_setting_info_dict(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _resp(make_settings_response(DEFAULT_AUDIO_SETTINGS)),
            _resp(make_settings_options_response(DEFAULT_AUDIO_OPTIONS)),
        ]
        settings = await vizio_tv.get_settings("audio")
        assert isinstance(settings, dict)
        assert "volume" in settings
        assert isinstance(settings["volume"], SettingInfo)
        assert settings["volume"].value == 25
        assert settings["volume"].hashval == 100

    async def test_get_setting(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(
            make_success_response(
                items=[
                    make_item("volume", 25, hashval=12345, item_type="T_VALUE_ABS_V1")
                ]
            )
        )
        info = await vizio_tv.get_setting("audio", "volume")
        assert isinstance(info, SettingInfo)
        assert info.value == 25
        assert info.hashval == 12345

    async def test_set_setting_with_hashval(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Caller-supplied hashval skips the GET — single round trip."""
        mock_client.return_value = _resp(make_success_response())
        await vizio_tv.set_setting("audio", "volume", 25, hashval=12345)
        # Exactly one call — the PUT.
        assert mock_client.call_count == 1
        body = _last_call_body(mock_client)
        assert body == {"VALUE": 25, "HASHVAL": 12345, "REQUEST": "MODIFY"}

    async def test_set_setting_no_hashval_does_get_then_put(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        ``get_setting`` fires two GETs (dynamic value + static options) so
        client-side validation has option/bounds data. With a numeric
        ``T_VALUE_V1`` setting and no bounds, validation passes through
        unchanged. Total: 2 GETs + 1 PUT.
        """
        mock_client.side_effect = [
            _resp(make_success_response(items=[make_item("volume", 20, hashval=999)])),
            _resp(make_success_response(items=[make_item("volume", 20, hashval=999)])),
            _resp(make_success_response()),
        ]
        await vizio_tv.set_setting("audio", "volume", 25)
        assert mock_client.call_count == 3
        # Final call is the PUT, with hashval from the GET.
        put_body = _last_call_body(mock_client)
        assert put_body == {"VALUE": 25, "HASHVAL": 999, "REQUEST": "MODIFY"}

    async def test_set_setting_retries_on_invalid_parameter(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Hashval race recovery (protocol-notes #13). The first PUT fails
        with invalid_parameter (stale hashval); we re-GET, re-PUT, succeed.
        Caller sees no exception. With the new options-fetch each
        ``get_setting`` is two GETs."""
        mock_client.side_effect = [
            _resp(make_success_response(items=[make_item("volume", 20, hashval=999)])),
            _resp(make_success_response(items=[make_item("volume", 20, hashval=999)])),
            VizioInvalidParameterError("stale hashval"),
            _resp(make_success_response(items=[make_item("volume", 20, hashval=1000)])),
            _resp(make_success_response(items=[make_item("volume", 20, hashval=1000)])),
            _resp(make_success_response()),
        ]
        await vizio_tv.set_setting("audio", "volume", 25)
        assert mock_client.call_count == 6
        # Final PUT used the fresh hashval.
        put_body = _last_call_body(mock_client)
        assert put_body["HASHVAL"] == 1000

    async def test_set_setting_retry_failure_propagates(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """If the second PUT also fails, propagate — don't loop forever."""
        mock_client.side_effect = [
            _resp(make_success_response(items=[make_item("volume", 20, hashval=999)])),
            _resp(make_success_response(items=[make_item("volume", 20, hashval=999)])),
            VizioInvalidParameterError("stale 1"),
            _resp(make_success_response(items=[make_item("volume", 20, hashval=1000)])),
            _resp(make_success_response(items=[make_item("volume", 20, hashval=1000)])),
            VizioInvalidParameterError("stale 2"),
        ]
        with pytest.raises(VizioInvalidParameterError):
            await vizio_tv.set_setting("audio", "volume", 25)

    async def test_set_setting_explicit_hashval_no_retry(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """When caller passes hashval=, we trust them — no auto-retry,
        full control over the failure path."""
        mock_client.side_effect = VizioInvalidParameterError("stale")
        with pytest.raises(VizioInvalidParameterError):
            await vizio_tv.set_setting("audio", "volume", 25, hashval=999)
        assert mock_client.call_count == 1

    async def test_set_setting_validates_list_value_against_options(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        Captured live from VHD24M-0810: dialogue_enhancer is a T_LIST_V1
        with options ('Off', 'Low', 'Medium', 'High'). Sending 'On' (not
        in options) returns INVALID_PARAMETER from the device. With
        client-side validation we surface a clearer error and avoid the
        round trip — and the message lists valid options so users can
        recover.
        """
        mock_client.return_value = _resp(
            make_success_response(
                items=[
                    {
                        "CNAME": "dialogue_enhancer",
                        "TYPE": "T_LIST_V1",
                        "NAME": "ClearDialog",
                        "VALUE": "Off",
                        "HASHVAL": 4284435521,
                        "ELEMENTS": ["Off", "Low", "Medium", "High"],
                    }
                ]
            )
        )
        with pytest.raises(VizioInvalidParameterError, match="not in options"):
            await vizio_tv.set_setting("audio", "dialogue_enhancer", "On")
        # No PUT issued — validation rejected before send.
        for call in mock_client.call_args_list:
            spec = call.args[0]
            assert spec.method == "GET"

    async def test_set_setting_canonicalizes_list_value_case(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        Case-insensitive match against the options set: ``'low'`` resolves
        to ``'Low'`` and the device receives the canonical form. Saves
        the user from caring about the device's preferred capitalization.
        """
        mock_client.side_effect = [
            _resp(
                make_success_response(
                    items=[
                        {
                            "CNAME": "dialogue_enhancer",
                            "TYPE": "T_LIST_V1",
                            "NAME": "ClearDialog",
                            "VALUE": "Off",
                            "HASHVAL": 1,
                            "ELEMENTS": ["Off", "Low", "Medium", "High"],
                        }
                    ]
                )
            ),
            _resp(
                make_success_response(
                    items=[
                        {
                            "CNAME": "dialogue_enhancer",
                            "TYPE": "T_LIST_V1",
                            "NAME": "ClearDialog",
                            "VALUE": "Off",
                            "HASHVAL": 1,
                            "ELEMENTS": ["Off", "Low", "Medium", "High"],
                        }
                    ]
                )
            ),
            _resp(make_success_response()),
        ]
        await vizio_tv.set_setting("audio", "dialogue_enhancer", "low")
        # PUT body must carry the canonical-case option.
        put_body = _last_call_body(mock_client)
        assert put_body["VALUE"] == "Low"


class TestSettingActions:
    """T_ACTION_V1 items fire with REQUEST: ACTION (protocol-notes #29).

    Verified live on M65Q7-H1 fw 1.720.9.1-1 — see
    ``tests/captured/settings_system_timers_blank_screen.json``.
    """

    @staticmethod
    def _action_item(hashval: int = 1578429529) -> dict:
        return make_item(
            "blank_screen",
            "T_ACTION_V1",
            item_type="T_ACTION_V1",
            name="Blank Screen",
            hashval=hashval,
        )

    async def test_trigger_does_get_then_action_put(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """No hashval supplied: one GET (raw leaf), then the ACTION PUT."""
        mock_client.side_effect = [
            _resp(make_success_response(items=[self._action_item()])),
            _resp(make_success_response()),
        ]
        await vizio_tv.trigger_setting_action("system/timers", "blank_screen")
        assert mock_client.call_count == 2
        put_body = _last_call_body(mock_client)
        assert put_body == {"REQUEST": "ACTION", "HASHVAL": 1578429529}

    async def test_trigger_with_hashval_skips_get(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(make_success_response())
        await vizio_tv.trigger_setting_action(
            "system/timers", "blank_screen", hashval=42
        )
        assert mock_client.call_count == 1
        assert _last_call_body(mock_client) == {"REQUEST": "ACTION", "HASHVAL": 42}

    async def test_trigger_with_hashval_does_not_retry(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Explicit hashval = caller owns the race; no auto-retry fires."""
        mock_client.side_effect = VizioInvalidParameterError("HASHVAL_ERROR")
        with pytest.raises(VizioInvalidParameterError):
            await vizio_tv.trigger_setting_action(
                "system/timers", "blank_screen", hashval=42
            )
        assert mock_client.call_count == 1  # the one PUT, no refetch

    async def test_trigger_missing_hashval_raises_response_error(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """An action leaf with no HASHVAL is a malformed device response."""
        mock_client.return_value = _resp(
            make_success_response(
                items=[
                    make_item(
                        "blank_screen",
                        "T_ACTION_V1",
                        item_type="T_ACTION_V1",
                        hashval=None,
                    )
                ]
            )
        )
        with pytest.raises(VizioResponseError, match="no HASHVAL"):
            await vizio_tv.trigger_setting_action("system/timers", "blank_screen")
        assert mock_client.call_count == 1  # the GET only, no PUT

    async def test_trigger_retries_on_stale_hashval(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """HASHVAL_ERROR from the PUT → re-GET, re-PUT once (notes #13)."""
        mock_client.side_effect = [
            _resp(make_success_response(items=[self._action_item(hashval=1)])),
            VizioInvalidParameterError("HASHVAL_ERROR"),
            _resp(make_success_response(items=[self._action_item(hashval=2)])),
            _resp(make_success_response()),
        ]
        await vizio_tv.trigger_setting_action("system/timers", "blank_screen")
        assert mock_client.call_count == 4
        assert _last_call_body(mock_client)["HASHVAL"] == 2

    async def test_trigger_retry_failure_propagates(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _resp(make_success_response(items=[self._action_item(hashval=1)])),
            VizioInvalidParameterError("stale 1"),
            _resp(make_success_response(items=[self._action_item(hashval=2)])),
            VizioInvalidParameterError("stale 2"),
        ]
        with pytest.raises(VizioInvalidParameterError):
            await vizio_tv.trigger_setting_action("system/timers", "blank_screen")

    async def test_trigger_on_value_leaf_raises_without_put(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """Firing ACTION at a value leaf is a caller bug — fail client-side."""
        mock_client.return_value = _resp(
            make_success_response(items=[make_item("volume", 20, hashval=999)])
        )
        with pytest.raises(VizioInvalidInputError, match="not an action item"):
            await vizio_tv.trigger_setting_action("audio", "volume")
        assert mock_client.call_count == 1  # the GET only, no PUT

    async def test_blank_screen_targets_timers_leaf(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = [
            _resp(make_success_response(items=[self._action_item()])),
            _resp(make_success_response()),
        ]
        await vizio_tv.blank_screen()
        put_kwargs = mock_client.call_args.kwargs
        assert put_kwargs["path_suffix"] == "/system/timers/blank_screen"
        assert _last_call_body(mock_client)["REQUEST"] == "ACTION"


# ===========================================================================
# Apps
# ===========================================================================


class TestApps:
    """Migration:
    - get_current_app(apps_list=) → get_current_app() (no apps_list arg —
      uses bundled+remote-fetched catalog internally)
    - get_current_app_config() → unchanged
    - launch_app(name, apps_list=) → launch_app(name)
    - launch_app_config(APP_ID, NAME_SPACE, MESSAGE) → launch_app_config(AppConfig)
    """

    async def test_get_current_app_known(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(
            make_current_app_response(app_id="3", name_space=2)
        )
        # Catalog lookup uses the bundled apps list. Hulu has APP_ID=3.
        result = await vizio_tv.get_current_app()
        # Hulu is in the bundled catalog; matched via NAME_SPACE 2↔4 if
        # needed. Non-strict assertion since exact catalog content varies.
        assert result is not None

    async def test_get_current_app_no_app(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(make_no_app_response())
        # When no app is running, get_current_app returns None per the
        # new contract — pyvizio returned the NO_APP_RUNNING sentinel.
        # Migration: caller compares against None instead of the magic string.
        assert await vizio_tv.get_current_app() is None

    async def test_get_current_app_config(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(
            make_current_app_response(app_id="1", name_space=3)
        )
        config = await vizio_tv.get_current_app_config()
        assert isinstance(config, AppConfig)
        assert config.app_id == "1"
        assert config.name_space == 3

    async def test_launch_app(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(make_success_response())
        await vizio_tv.launch_app("Netflix")
        # Body shape: VALUE wraps APP_ID, NAME_SPACE, MESSAGE.
        body = _last_call_body(mock_client)
        assert "VALUE" in body
        assert "APP_ID" in body["VALUE"]

    async def test_launch_app_config(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(make_success_response())
        await vizio_tv.launch_app_config(
            AppConfig(app_id="42", name_space=2, message="msg")
        )
        body = _last_call_body(mock_client)
        assert body["VALUE"] == {
            "APP_ID": "42",
            "NAME_SPACE": 2,
            "MESSAGE": "msg",
        }

    async def test_apps_unsupported_on_soundbar(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        with pytest.raises(VizioUnsupportedError):
            await vizio_soundbar.get_current_app()
        mock_client.assert_not_called()


# ===========================================================================
# Device info
# ===========================================================================


class TestDeviceInfo:
    """Migration:
    - get_esn() / get_serial_number() / get_version() → unchanged signature,
      but raise on error instead of returning None
    - get_model_name() → unchanged
    - get_device_info() → NEW aggregate that returns DeviceInfo
    """

    @pytest.mark.parametrize(
        "method,cname,value",
        [
            ("get_esn", "esn", "VIZIO-ESN-123"),
            ("get_serial_number", "serial_number", "SN12345"),
            ("get_version", "version", "4.0.20.1"),
        ],
    )
    async def test_device_info_primary(
        self,
        vizio_tv: Vizio,
        mock_client: AsyncMock,
        method: str,
        cname: str,
        value: str,
    ) -> None:
        mock_client.return_value = _resp(
            make_success_response(items=[make_item(cname, value)])
        )
        result = await getattr(vizio_tv, method)()
        assert result == value

    async def test_get_model_name_tv(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(
            make_device_info_response({"MODEL_NAME": "V505-G9"})
        )
        assert await vizio_tv.get_model_name() == "V505-G9"

    async def test_get_model_name_soundbar(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        # Soundbars use NAME instead of MODEL_NAME.
        mock_client.return_value = _resp(
            make_device_info_response({"NAME": "VIZIO SB3651"})
        )
        assert await vizio_soundbar.get_model_name() == "VIZIO SB3651"

    async def test_identity_via_aggregate_endpoint(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        When deviceinfo doesn't carry a field (older firmware), the
        aggregate ``tv_information`` endpoint is the fallback. Modern
        firmware (verified on VHD24M-0810 fw 3.720.9.1-1) returns all
        identity fields as items in one response; per-field child paths
        return URI_NOT_FOUND. The library probes deviceinfo first, then
        fetches the aggregate once and serves subsequent identity calls
        from cache rather than making N additional round trips.
        """
        mock_client.side_effect = [
            # deviceinfo probe: no SYSTEM_INFO, so serial/version fall through.
            _resp(make_device_info_response({})),
            _resp(
                make_success_response(
                    items=[
                        make_item("tv_name", "Test TV"),
                        make_item("serial_number", "TEST00000000001"),
                        make_item("model_name", "VHD24M-0810"),
                        make_item("firmware", "3.720.9.1-1"),
                    ]
                )
            ),
        ]

        # deviceinfo probe (miss) + aggregate fetch = 2 calls.
        assert await vizio_tv.get_serial_number() == "TEST00000000001"
        assert mock_client.call_count == 2
        # ``firmware`` cname is the modern-firmware alias for ``version``.
        # Served from the deviceinfo + aggregate caches — no new call.
        assert await vizio_tv.get_version() == "3.720.9.1-1"
        assert mock_client.call_count == 2

    async def test_serial_and_version_from_deviceinfo_unauthenticated(
        self, mock_client: AsyncMock
    ) -> None:
        """
        Serial number and version come from the unauthenticated
        deviceinfo SYSTEM_INFO block — no auth token, no aggregate /
        per-field round trip. Verified live on VHD24M-0810 fw
        3.720.9.1-1: the deviceinfo serial equals the auth-path serial.
        """
        # A TV with NO auth token — the auth-gated menu_native identity
        # paths would raise VizioAuthError, so a returned value proves it
        # came from deviceinfo.
        tv = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV, auth_token=None)
        try:
            mock_client.return_value = _resp(
                make_device_info_response(
                    {
                        "MODEL_NAME": "VHD24M-0810",
                        "SYSTEM_INFO": {
                            "SERIAL_NUMBER": "24LMV5U2RB05077",
                            "VERSION": "3.720.9.1-1",
                        },
                    }
                )
            )
            assert await tv.get_serial_number() == "24LMV5U2RB05077"
            assert await tv.get_version() == "3.720.9.1-1"
            # One deviceinfo GET, shared across both reads; no auth path hit.
            assert mock_client.call_count == 1
            assert _last_call_paths(mock_client) == ("/state/device/deviceinfo",)
        finally:
            await tv.aclose()

    async def test_identity_propagates_transport_errors(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        An unreachable or malfunctioning device must not read as "this
        firmware doesn't expose the field". Returning "" for both makes the
        sentinel ambiguous — callers cannot tell a TV with no serial number
        from a TV that is down.
        """
        mock_client.side_effect = VizioConnectionError("device unreachable")
        with pytest.raises(VizioConnectionError):
            await vizio_tv.get_serial_number()

    async def test_identity_propagates_malformed_responses(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """A garbage envelope is an error, not an absent field."""
        mock_client.side_effect = VizioResponseError("garbage envelope")
        with pytest.raises(VizioResponseError):
            await vizio_tv.get_version()

    async def test_identity_empty_when_firmware_lacks_field(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """URI_NOT_FOUND everywhere means the field genuinely isn't exposed."""
        from vizaio.errors import VizioNotFoundError

        mock_client.side_effect = VizioNotFoundError("no such uri")
        assert await vizio_tv.get_serial_number() == ""

    async def test_esn_never_reads_deviceinfo(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        ESN has no deviceinfo equivalent, so get_esn() must not be served
        from a deviceinfo probe — it goes straight to the aggregate.
        """
        from vizaio.errors import VizioNotFoundError

        mock_client.side_effect = [
            # Aggregate carries the ESN directly — reached without any
            # preceding deviceinfo probe.
            _resp(make_success_response(items=[make_item("esn", "VIZIO-ESN-123")])),
            VizioNotFoundError("per-field not needed"),
        ]
        assert await vizio_tv.get_esn() == "VIZIO-ESN-123"
        # Exactly one call (the aggregate) — no deviceinfo probe up front.
        assert mock_client.call_count == 1
        assert _last_call_paths(mock_client)[0].endswith("/tv_information")

    async def test_identity_falls_back_to_per_field_when_aggregate_missing(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        Older firmware doesn't expose the aggregate ``tv_information``
        endpoint at all. The per-field endpoints (with their own
        firmware-multi-path fallback) must continue to work.
        """
        from vizaio.errors import VizioNotFoundError

        mock_client.side_effect = [
            # deviceinfo probe misses (no SERIAL_NUMBER on old firmware)...
            VizioNotFoundError("deviceinfo unavailable"),
            # ...aggregate not exposed...
            VizioNotFoundError("aggregate not exposed"),
            # ...per-field endpoint succeeds.
            _resp(make_success_response(items=[make_item("serial_number", "OLD-SN")])),
        ]
        result = await vizio_tv.get_serial_number()
        assert result == "OLD-SN"

    async def test_identity_returns_empty_when_field_unavailable(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        When a field is exposed neither in the aggregate nor at the
        per-field endpoint (e.g., ESN on this generation of firmware),
        the getter returns ``""`` rather than raising. ``get_device_info``
        depends on this graceful-degrade behavior.
        """
        from vizaio.errors import VizioNotFoundError

        # Aggregate succeeds but lacks 'esn'; per-field endpoint then 404s.
        mock_client.side_effect = [
            _resp(
                make_success_response(
                    items=[make_item("serial_number", "SN")]  # no 'esn' here
                )
            ),
            VizioNotFoundError("URI not found"),
        ]
        assert await vizio_tv.get_esn() == ""

    async def test_version_from_deviceinfo_honors_firmware_alias(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        The deviceinfo probe walks (cname, *aliases) like the aggregate
        path: a SYSTEM_INFO that keys the value under ``firmware`` (the
        alias) rather than ``version`` (the cname) is still read from
        deviceinfo instead of falling through to the auth-gated path.
        """
        mock_client.return_value = _resp(
            make_device_info_response({"SYSTEM_INFO": {"FIRMWARE": "9.9.9"}})
        )
        assert await vizio_tv.get_version() == "9.9.9"
        # deviceinfo only — the alias matched, so the aggregate is never hit.
        assert mock_client.call_count == 1
        assert _last_call_paths(mock_client) == ("/state/device/deviceinfo",)

    async def test_deviceinfo_failure_cached_not_refetched(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        An unreachable deviceinfo is remembered for the session: one
        ``get_device_info()`` issues a single deviceinfo GET, not one per
        deviceinfo-backed field (model + serial + version), and the
        outcome is not re-fetched on later reads.
        """
        mock_client.side_effect = VizioConnectionError("device down")

        info = await vizio_tv.get_device_info()
        # Everything degrades gracefully to empty.
        assert info.model == "" and info.serial_number == "" and info.version == ""

        deviceinfo_calls = [
            c
            for c in mock_client.call_args_list
            if c.args[0].paths == ("/state/device/deviceinfo",)
        ]
        assert len(deviceinfo_calls) == 1, (
            "deviceinfo failure should be cached, not re-fetched per field"
        )

        # A later identity read still doesn't re-hit deviceinfo, and now
        # surfaces the transport failure rather than masking it as "".
        with pytest.raises(VizioConnectionError):
            await vizio_tv.get_serial_number()
        deviceinfo_calls = [
            c
            for c in mock_client.call_args_list
            if c.args[0].paths == ("/state/device/deviceinfo",)
        ]
        assert len(deviceinfo_calls) == 1

    async def test_concurrent_deviceinfo_reads_coalesce(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """
        Concurrent deviceinfo-backed getters issue ONE GET, not one each:
        the lock coalesces callers that all pass the empty-cache check
        while the first fetch is in flight.
        """
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def slow_request(spec: object, **_kwargs: object) -> Response:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return _resp(
                make_device_info_response(
                    {"MODEL_NAME": "X", "SYSTEM_INFO": {"VERSION": "1.2.3"}}
                )
            )

        mock_client.side_effect = slow_request
        task = asyncio.gather(vizio_tv.get_model_name(), vizio_tv.get_version())
        await started.wait()  # first fetch is in flight, holding the lock
        release.set()
        model, version = await task

        assert model == "X"
        assert version == "1.2.3"
        assert calls == 1, "concurrent deviceinfo reads should coalesce to one GET"

    async def test_get_state_extended(self, vizio_tv: Vizio) -> None:
        """
        ``/state_extended`` returns a non-standard envelope (flat keys,
        no STATUS/ITEMS). Captured live from VHD24M-0810 fw 3.720.9.1-1.
        Verifies parser pulls the right typed fields from the unique shape.
        """
        from vizaio import StateExtended

        raw = {
            "ERRORS": [],
            "URI": "/state_extended",
            "DEVICE_NAME": "Test TV",
            "POWER_STATUS": {"VALUE": 1},
            "POWER_MODE": {"VALUE": "Quick Start", "HASHVAL": 3026334404},
            "APP_CURRENT": {
                "APP_ID": "1",
                "MESSAGE": '{"app":"home","bundle":"bundles/home"}',
                "NAME_SPACE": 4,
            },
            "CURRENT_INPUT": {"NAME": "SMARTCAST", "HASHVAL": 3009117460},
            "SCREEN_MODE": "Full screen",
            "MEDIA_STATE": "MediaState::Stopped",
        }
        # state_extended bypasses the standard envelope parser and uses
        # SmartCastClient.request_raw_json. Stub that with the captured
        # payload.
        from unittest.mock import AsyncMock

        # Replace the bound method on this Vizio's client instance.
        vizio_tv._client.request_raw_json = AsyncMock(return_value=raw)  # type: ignore[method-assign]
        s = await vizio_tv.get_state_extended()

        assert isinstance(s, StateExtended)
        assert s.power_on is True
        assert s.power_mode == "Quick Start"
        assert s.current_input == "SMARTCAST"
        assert s.current_input_hashval == 3009117460
        assert s.current_app is not None
        assert s.current_app.app_id == "1"
        assert s.current_app.name_space == 4
        assert s.screen_mode == "Full screen"
        assert s.media_state == "MediaState::Stopped"
        assert s.device_name == "Test TV"
        assert s.errors == ()

    async def test_get_state_extended_tolerates_missing_fields(
        self, vizio_tv: Vizio
    ) -> None:
        """
        Older firmware may not populate every field. The parser
        degrades gracefully — no exceptions — so polling keeps working
        across firmware revisions.
        """
        from unittest.mock import AsyncMock

        # Minimal payload — most fields missing.
        vizio_tv._client.request_raw_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"URI": "/state_extended", "POWER_STATUS": {"VALUE": 0}}
        )
        s = await vizio_tv.get_state_extended()

        assert s.power_on is False
        assert s.power_mode == ""
        assert s.current_input == ""
        assert s.current_input_hashval is None
        assert s.current_app is None
        assert s.screen_mode == ""
        assert s.media_state == ""

    async def test_get_device_info_aggregate(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """get_device_info fetches all identity fields in one call."""
        from vizaio import DeviceInfo

        # Mock multiple endpoints needed for full DeviceInfo.
        mock_client.side_effect = [
            _resp(make_device_info_response({"MODEL_NAME": "V505-G9"})),
            _resp(make_success_response(items=[make_item("serial_number", "SN")])),
            _resp(make_success_response(items=[make_item("esn", "ESN")])),
            _resp(make_success_response(items=[make_item("version", "4.0")])),
            _resp(make_inputs_list_response(DEFAULT_TV_INPUTS)),
            # get_inputs now also fetches current_input to populate
            # is_current correctly across firmware revisions.
            _resp(make_current_input_response("current_input", "HDMI-1", 5)),
        ]
        info = await vizio_tv.get_device_info()
        assert isinstance(info, DeviceInfo)
        assert info.model == "V505-G9"
        assert info.serial_number == "SN"

    async def test_get_versions(self, vizio_tv: Vizio) -> None:
        """
        GET /system/versions: {STATUS, ITEM:{VALUE:{<version map>}}}.
        Shape captured live from VHD24M-0810 fw 3.720.9.1-1 (keys verbatim,
        incl. spaces). Common fields typed; everything on .raw.
        """
        from unittest.mock import AsyncMock

        from vizaio import SystemVersions

        raw = {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEM": {
                "TYPE": "T_JSON_OBJECT_V1",
                "VALUE": {
                    "ESN": "LMV5U2RB2405077",
                    "FIRMWARE": "3.720.9.1-1",
                    "SCPL": "3.4.3-2614.0002",
                    "SERIAL NUMBER": "24LMV5U2RB05077",
                    "acr": "3.5.1106",
                },
            },
            "URI": "/system/versions",
        }
        vizio_tv._client.request_raw_json = AsyncMock(return_value=raw)  # type: ignore[method-assign]
        v = await vizio_tv.get_versions()
        assert isinstance(v, SystemVersions)
        assert v.firmware == "3.720.9.1-1"
        assert v.serial_number == "24LMV5U2RB05077"
        assert v.esn == "LMV5U2RB2405077"
        assert v.scpl == "3.4.3-2614.0002"
        # Full device-cased map preserved.
        assert v.raw["acr"] == "3.5.1106"

    async def test_get_versions_tolerates_missing(self, vizio_tv: Vizio) -> None:
        from unittest.mock import AsyncMock

        vizio_tv._client.request_raw_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"STATUS": {"RESULT": "SUCCESS"}, "URI": "/system/versions"}
        )
        v = await vizio_tv.get_versions()
        assert v.firmware == ""
        assert v.raw == {}

    async def test_is_pin_default_true(self, vizio_tv: Vizio) -> None:
        """GET /pin/is_pin_default -> {ITEM:{VALUE: true}} (captured live)."""
        from unittest.mock import AsyncMock

        vizio_tv._client.request_raw_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"STATUS": {"RESULT": "SUCCESS"}, "ITEM": {"VALUE": True}}
        )
        assert await vizio_tv.is_pin_default() is True

    async def test_is_pin_default_false(self, vizio_tv: Vizio) -> None:
        from unittest.mock import AsyncMock

        vizio_tv._client.request_raw_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"STATUS": {"RESULT": "SUCCESS"}, "ITEM": {"VALUE": False}}
        )
        assert await vizio_tv.is_pin_default() is False

    async def test_is_pin_default_missing_item(self, vizio_tv: Vizio) -> None:
        from unittest.mock import AsyncMock

        vizio_tv._client.request_raw_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"STATUS": {"RESULT": "SUCCESS"}}
        )
        assert await vizio_tv.is_pin_default() is False


# ===========================================================================
# Battery (Crave 360 only)
# ===========================================================================


class TestBattery:
    """Migration:
    - get_charging_status() -> int | None → get_charging_status() -> ChargingStatus
    - get_battery_level() -> int | None → get_battery_level() -> int (raises)
    - Both raise VizioUnsupportedError on TVs/soundbars
    """

    async def test_get_battery_level(
        self, vizio_crave: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(make_battery_level_response(75))
        assert await vizio_crave.get_battery_level() == 75

    async def test_get_charging_status_returns_enum(
        self, vizio_crave: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(make_charging_status_response(1))
        status = await vizio_crave.get_charging_status()
        assert status is ChargingStatus.CHARGING

    @pytest.mark.parametrize(
        "fixture_name",
        ["vizio_tv", "vizio_soundbar"],
    )
    async def test_battery_unsupported_off_crave(
        self,
        request: pytest.FixtureRequest,
        mock_client: AsyncMock,
        fixture_name: str,
    ) -> None:
        device = request.getfixturevalue(fixture_name)
        with pytest.raises(VizioUnsupportedError):
            await device.get_battery_level()
        with pytest.raises(VizioUnsupportedError):
            await device.get_charging_status()
        mock_client.assert_not_called()


# ===========================================================================
# Pairing — context manager
# ===========================================================================


class TestPairSession:
    """Migration:
    - start_pair() / pair() / stop_pair() → pair_session(device_id, name)
      context manager
    - Auto-cancels on exception or early exit
    - Doesn't cancel on successful complete()
    """

    async def test_successful_complete(self, mock_client: AsyncMock) -> None:
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [
            _resp(make_pair_begin_response(1, 54321)),
            _resp(make_pair_finish_response("new-token")),
        ]
        async with v.pair_session(
            device_id="ha-coord", device_name="HomeAssistant"
        ) as session:
            token = await session.complete(pin="1234")
        assert token == "new-token"
        # No CANCEL_PAIR call — successful complete suppresses it.
        endpoints_hit = _all_call_paths(mock_client)
        assert ("/pairing/cancel",) not in endpoints_hit

    async def test_exception_inside_block_cancels(self, mock_client: AsyncMock) -> None:
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [
            _resp(make_pair_begin_response(1, 54321)),
            _resp(make_success_response()),  # cancel response
        ]
        with pytest.raises(RuntimeError, match="user-error"):
            async with v.pair_session(
                device_id="ha-coord", device_name="HomeAssistant"
            ):
                raise RuntimeError("user-error")
        endpoints_hit = _all_call_paths(mock_client)
        assert endpoints_hit[-1] == ("/pairing/cancel",)

    async def test_complete_failure_cancels(self, mock_client: AsyncMock) -> None:
        """If complete() itself raises (wrong PIN), the context manager
        still cancels on exit so the device doesn't get stuck."""
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [
            _resp(make_pair_begin_response(1, 54321)),
            VizioInvalidParameterError("wrong PIN"),
            _resp(make_success_response()),  # cancel response
        ]
        with pytest.raises(VizioInvalidParameterError):
            async with v.pair_session(
                device_id="ha-coord", device_name="HomeAssistant"
            ) as session:
                await session.complete(pin="0000")
        endpoints_hit = _all_call_paths(mock_client)
        assert endpoints_hit[-1] == ("/pairing/cancel",)

    async def test_complete_never_called_cancels(self, mock_client: AsyncMock) -> None:
        """User opens the context but never calls complete() (e.g., user
        Ctrl-C'd at the PIN prompt). Device should be canceled."""
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [
            _resp(make_pair_begin_response(1, 54321)),
            _resp(make_success_response()),  # cancel
        ]
        async with v.pair_session(device_id="ha-coord", device_name="HomeAssistant"):
            pass
        endpoints_hit = _all_call_paths(mock_client)
        assert endpoints_hit[-1] == ("/pairing/cancel",)

    async def test_session_exposes_challenge(self, mock_client: AsyncMock) -> None:
        """Caller can inspect the challenge details — e.g., for showing
        them to the user before complete() is called."""
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [
            _resp(make_pair_begin_response(1, 54321)),
            _resp(make_success_response()),
        ]
        async with v.pair_session(
            device_id="ha-coord", device_name="HomeAssistant"
        ) as session:
            assert session.challenge.challenge_type == 1
            assert session.challenge.token == 54321

    async def test_begin_pair_failure_cancels(self, mock_client: AsyncMock) -> None:
        """If begin_pair raises after the HTTP request is sent (e.g., malformed
        response), the device is left in pairing mode. __aenter__ must
        cancel so the device doesn't get stuck."""
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [
            VizioResponseError("malformed begin_pair response"),
            _resp(make_success_response()),  # cancel response
        ]
        with pytest.raises(VizioResponseError):
            async with v.pair_session(
                device_id="ha-coord", device_name="HomeAssistant"
            ):
                pass
        endpoints_hit = _all_call_paths(mock_client)
        assert endpoints_hit[-1] == ("/pairing/cancel",)


class TestBeginPair:
    """Stateless begin_pair: one-shot, no session lifecycle."""

    async def test_returns_challenge(self, mock_client: AsyncMock) -> None:
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [_resp(make_pair_begin_response(1, 54321))]
        challenge = await v.begin_pair(device_id="test", device_name="TestApp")
        assert challenge.challenge_type == 1
        assert challenge.token == 54321

    async def test_propagates_error(self, mock_client: AsyncMock) -> None:
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [VizioInvalidParameterError("bad")]
        with pytest.raises(VizioInvalidParameterError):
            await v.begin_pair(device_id="test", device_name="TestApp")


class TestFinishPair:
    """Stateless finish_pair: one-shot, no session lifecycle."""

    async def test_returns_auth_token(self, mock_client: AsyncMock) -> None:
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [_resp(make_pair_finish_response("TOK-123"))]
        token = await v.finish_pair(
            device_id="test",
            challenge=PairChallenge(challenge_type=1, token=54321),
            pin="1234",
        )
        assert token == "TOK-123"

    async def test_propagates_error(self, mock_client: AsyncMock) -> None:
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [VizioAuthError("PAIRING_DENIED")]
        with pytest.raises(VizioAuthError):
            await v.finish_pair(
                device_id="test",
                challenge=PairChallenge(challenge_type=1, token=54321),
                pin="0000",
            )


class TestCancelPair:
    """Stateless cancel_pair: best-effort, swallows VizioError."""

    async def test_succeeds(self, mock_client: AsyncMock) -> None:
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [_resp(make_success_response())]
        await v.cancel_pair(device_id="test", device_name="TestApp")

    async def test_swallows_error(self, mock_client: AsyncMock) -> None:
        v = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV)
        mock_client.side_effect = [VizioAuthError("device refused")]
        # Should not raise — cancel is best-effort.
        await v.cancel_pair(device_id="test", device_name="TestApp")


# ===========================================================================
# Auth behavior
# ===========================================================================


class TestAuth:
    """Migration:
    - TV with empty auth → raised at first request, now raises at
      construction-time the same VizioAuthError class
    - Soundbar/Crave succeed without auth (capability profile knows)
    """

    async def test_tv_empty_auth_raises_on_request(
        self, mock_client: AsyncMock
    ) -> None:
        tv = Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV, auth_token=None)
        with pytest.raises(VizioAuthError):
            await tv.get_power_state()
        # No HTTP call made — fail-fast.
        mock_client.assert_not_called()

    async def test_soundbar_empty_auth_succeeds(
        self, vizio_soundbar: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.return_value = _resp(make_power_response(1))
        assert await vizio_soundbar.get_power_state() is True


# ===========================================================================
# Health / connection probes
# ===========================================================================


class TestHealth:
    """Migration:
    - can_connect_no_auth_check() → ping() (raises on failure)
    - can_connect_with_auth_check() → ping_auth() (raises on failure)
    """

    async def test_ping_succeeds(self, vizio_tv: Vizio, mock_client: AsyncMock) -> None:
        mock_client.return_value = _resp(make_device_info_response({}))
        await vizio_tv.ping()  # No exception.

    async def test_ping_raises_on_unreachable(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        mock_client.side_effect = VizioConnectionError("unreachable")
        with pytest.raises(VizioConnectionError):
            await vizio_tv.ping()

    async def test_ping_auth_uses_auth(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        # Cheap settings GET that requires auth.
        mock_client.return_value = _resp(make_settings_response(DEFAULT_AUDIO_SETTINGS))
        await vizio_tv.ping_auth()


# ===========================================================================
# Issue regression tests
# ===========================================================================


class TestIssueRegressions:
    """Each test corresponds to a pyvizio open issue that the redesign
    addresses. Triaged in docs/protocol-notes.md and via #33."""

    async def test_issue_163_power_on_does_not_invert(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """pyvizio #163: 'Power On command turns power Off'. Asserts
        power_on sends CODE 1 (on), not CODE 0 (off). Sister test for
        power_off above asserts the inverse."""
        mock_client.return_value = _resp(make_key_press_response())
        await vizio_tv.power_on()
        assert _last_call_body(mock_client)["KEYLIST"][0]["CODE"] == 1

    async def test_issue_135_firmware_update_endpoint_fallback(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """pyvizio #135: 'Some sources no longer work with HomeAssistant'
        after firmware update. Cause: settings endpoint moved paths.
        Fix: EndpointSpec.paths is a tuple; SmartCastClient tries each
        in order, falling through on VizioNotFoundError (the response's
        item shape did not match this firmware revision).

        Path-level fallback is exercised in test_client.py. Here we
        verify the high-level method returns successfully when only one
        of the candidate paths has the data."""
        mock_client.return_value = _resp(
            make_success_response(items=[make_item("esn", "ALT-ESN-FOUND")])
        )
        result = await vizio_tv.get_esn()
        assert result == "ALT-ESN-FOUND"

    async def test_issue_152_hdmi_app_returns_none(
        self, vizio_tv: Vizio, mock_client: AsyncMock
    ) -> None:
        """pyvizio #152: 'UNKNOWN_APP reported whenever the TV is playing
        HDMI inputs'. With None-on-no-app, callers can compose:
        get_current_app() returns None → fall back to get_current_input()."""
        # When TV is on HDMI, current_app returns null/empty.
        mock_client.return_value = _resp(make_no_app_response())
        result = await vizio_tv.get_current_app()
        # Migration: returns None, not the string 'UNKNOWN_APP'.
        assert result is None
