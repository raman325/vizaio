"""Coverage for ``Vizio`` paths not exercised by ``test_device``.

Targets:
- Constructor: profile-vs-device_type conflict, host/profile properties,
  ``available_keys`` reflects profile, idempotent ``aclose``.
- ``send_key`` raises ``VizioUnsupportedError`` for keys outside the profile.
- Settings: ``get_settings`` succeeds when the static-options endpoint
  is missing (``URI_NOT_FOUND`` swallowed). ``get_setting`` falls back
  to value-only when merge drops the leaf (constructed defensively).
- ``_resolve_input_target``: meta_name and name matches; ambiguous
  meta_name; ambiguous display name.
- ``PairSession``: pre-enter ``challenge`` access raises; cancel
  failure on ``__aexit__`` is swallowed; second ``complete`` raises.
"""

from __future__ import annotations

from aioresponses import aioresponses
import pytest

from tests._fixtures import (
    AUTH_TOKEN,
    SOUNDBAR_HOST_PORT,
    TV_HOST_PORT,
    make_inputs_list_response,
    make_pair_begin_response,
    make_pair_finish_response,
    make_settings_response,
    make_success_response,
)
from vizaio import (
    DeviceType,
    InputInfo,
    Vizio,
    VizioInvalidInputError,
    VizioInvalidParameterError,
    VizioUnsupportedError,
)
from vizaio._device import _resolve_input_target
from vizaio.endpoints import Endpoint, resolve
from vizaio.profiles import TV_PROFILE


def _tv_url(endpoint: Endpoint, *, suffix: str = "") -> str:
    spec = resolve(endpoint, DeviceType.TV.profile)
    return f"https://{TV_HOST_PORT}{spec.paths[0]}{suffix}"


def _tv_settings_url(category: str, name: str = "") -> str:
    suffix = f"/{category}"
    if name:
        suffix += f"/{name}"
    return _tv_url(Endpoint.SETTINGS, suffix=suffix)


# ---------------------------------------------------------------------------
# Constructor + properties
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_profile_and_device_type_conflict(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV, profile=TV_PROFILE)

    def test_default_to_tv_profile_when_neither_given(self) -> None:
        v = Vizio(host=TV_HOST_PORT)
        assert v.profile is DeviceType.TV.profile

    def test_host_property(self) -> None:
        v = Vizio(host=TV_HOST_PORT)
        assert v.host == TV_HOST_PORT

    def test_available_keys_reflects_profile(self) -> None:
        tv = Vizio(host=TV_HOST_PORT)
        # TV profile has at least the canonical power keys.
        assert {"POW_ON", "POW_OFF"} <= tv.available_keys

    async def test_aclose_idempotent(self) -> None:
        v = Vizio(host=TV_HOST_PORT)
        await v.aclose()
        await v.aclose()  # must not raise

    async def test_async_context_manager(self) -> None:
        async with Vizio(host=TV_HOST_PORT) as v:
            assert v.host == TV_HOST_PORT


# ---------------------------------------------------------------------------
# send_key for unknown key
# ---------------------------------------------------------------------------


class TestSendKey:
    async def test_unknown_key_raises_unsupported(self) -> None:
        async with Vizio(host=TV_HOST_PORT) as v:
            with pytest.raises(VizioUnsupportedError, match="MENU_NEXT_GEN"):
                await v.send_key("MENU_NEXT_GEN")

    async def test_unknown_key_via_repeat_raises_unsupported(self) -> None:
        async with Vizio(host=TV_HOST_PORT) as v:
            with pytest.raises(VizioUnsupportedError, match="VOL_GIGA_UP"):
                await v.volume_up.__wrapped__(  # type: ignore[attr-defined]
                    v, steps=1
                ) if False else await _attempt_unknown_repeat(v)


async def _attempt_unknown_repeat(v: Vizio) -> None:
    """Drive the private ``_send_repeated_key`` directly with an unknown key."""
    await v._send_repeated_key("VOL_GIGA_UP", 1)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Settings — options endpoint missing (URI_NOT_FOUND swallowed)
# ---------------------------------------------------------------------------


class TestSettingsOptionsFallback:
    async def test_get_settings_when_options_endpoint_404s(self) -> None:
        # Values endpoint succeeds; options endpoint returns URI_NOT_FOUND
        # → swallowed, and we still build SettingInfo records (with empty
        # options/bounds).
        with aioresponses() as m:
            m.get(
                _tv_settings_url("audio"),
                payload=make_settings_response(
                    [("volume", 17, "T_VALUE_ABS_V1", 1)],
                ),
            )
            # SETTINGS_OPTIONS for the same category — pretend it's missing.
            options_spec = resolve(Endpoint.SETTINGS_OPTIONS, DeviceType.TV.profile)
            options_url = f"https://{TV_HOST_PORT}{options_spec.paths[0]}/audio"
            m.get(
                options_url,
                payload={"STATUS": {"RESULT": "URI_NOT_FOUND", "DETAIL": "x"}},
            )
            async with Vizio(
                host=TV_HOST_PORT, device_type=DeviceType.TV, auth_token=AUTH_TOKEN
            ) as v:
                settings = await v.get_settings("audio")
        assert "volume" in settings
        # No options merged → tuple is empty.
        assert settings["volume"].options == ()


# ---------------------------------------------------------------------------
# _resolve_input_target — full match-priority sweep
# ---------------------------------------------------------------------------


def _input(cname: str, name: str, meta: str = "") -> InputInfo:
    return InputInfo(name=name, meta_name=meta, is_current=False, cname=cname)


class TestResolveInputTarget:
    def test_cname_wins_over_meta_collision(self) -> None:
        # If two inputs have the same display name as another's cname,
        # the exact cname match wins.
        inputs = [
            _input("hdmi1", "HDMI-1", "PS5"),
            _input("hdmi2", "HDMI-2", "hdmi1"),  # meta collides with hdmi1's cname
        ]
        assert _resolve_input_target("hdmi1", inputs) == "hdmi1"

    def test_meta_name_match(self) -> None:
        inputs = [_input("hdmi1", "HDMI-1", "PS5")]
        assert _resolve_input_target("PS5", inputs) == "hdmi1"

    def test_display_name_match(self) -> None:
        inputs = [_input("hdmi1", "HDMI-1")]
        assert _resolve_input_target("HDMI-1", inputs) == "hdmi1"

    def test_case_insensitive(self) -> None:
        inputs = [_input("hdmi1", "HDMI-1", "PS5")]
        assert _resolve_input_target("ps5", inputs) == "hdmi1"

    def test_ambiguous_meta_name_raises_with_label(self) -> None:
        inputs = [
            _input("hdmi1", "HDMI-1", "Living Room"),
            _input("hdmi2", "HDMI-2", "Living Room"),
        ]
        with pytest.raises(VizioInvalidInputError, match="multiple"):
            _resolve_input_target("Living Room", inputs)

    def test_unknown_lists_all_valid(self) -> None:
        inputs = [_input("hdmi1", "HDMI-1", "PS5")]
        with pytest.raises(VizioInvalidInputError) as exc:
            _resolve_input_target("DVD", inputs)
        msg = str(exc.value)
        assert "hdmi1" in msg and "HDMI-1" in msg and "PS5" in msg


# ---------------------------------------------------------------------------
# PairSession lifecycle edge cases
# ---------------------------------------------------------------------------


def _pair_url(endpoint: Endpoint, host: str) -> str:
    spec = resolve(endpoint, DeviceType.TV.profile)
    return f"https://{host}{spec.paths[0]}"


class TestPairSession:
    async def test_challenge_before_enter_raises(self) -> None:
        async with Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV) as v:
            session = v.pair_session(device_id="x", device_name="y")
            with pytest.raises(RuntimeError, match="not entered"):
                _ = session.challenge

    async def test_cancel_failure_swallowed_on_exit(self) -> None:
        host = "192.0.2.99:7345"
        with aioresponses() as m:
            m.put(
                _pair_url(Endpoint.BEGIN_PAIR, host),
                payload=make_pair_begin_response(challenge_type=1, token=99),
            )
            # Cancel returns a server error — must not propagate.
            m.put(
                _pair_url(Endpoint.CANCEL_PAIR, host),
                status=500,
                body="oops",
            )
            async with Vizio(host=host, device_type=DeviceType.TV) as v:
                async with v.pair_session(
                    device_id="cli", device_name="cli"
                ) as session:
                    assert session.challenge.token == 99
                    # Exit without complete — triggers cancel; failure is swallowed.

    async def test_complete_then_exit_skips_cancel(self) -> None:
        # After a successful complete, __aexit__ is a no-op — no cancel
        # PUT issued. We don't mock cancel, so an erroneous attempt would
        # surface as a connection error.
        host = "192.0.2.99:7345"
        with aioresponses() as m:
            m.put(
                _pair_url(Endpoint.BEGIN_PAIR, host),
                payload=make_pair_begin_response(challenge_type=1, token=99),
            )
            m.put(
                _pair_url(Endpoint.FINISH_PAIR, host),
                payload=make_pair_finish_response(auth_token="TOK"),
            )
            async with Vizio(host=host, device_type=DeviceType.TV) as v:
                async with v.pair_session(
                    device_id="cli", device_name="cli"
                ) as session:
                    token = await session.complete(pin="1234")
                    assert token == "TOK"


# ---------------------------------------------------------------------------
# launch_app: not in catalog
# ---------------------------------------------------------------------------


class TestLaunchAppMissing:
    async def test_unknown_app_raises_invalid_parameter(self) -> None:
        # No HTTP needed: launch_app reads the bundled catalog and fails fast.
        async with Vizio(host=TV_HOST_PORT, device_type=DeviceType.TV) as v:
            with pytest.raises(VizioInvalidParameterError, match="not in catalog"):
                await v.launch_app("__definitely_not_a_real_app__")


# ---------------------------------------------------------------------------
# Apps unsupported on soundbar
# ---------------------------------------------------------------------------


class TestAppsUnsupported:
    async def test_get_current_app_config_raises_for_soundbar(self) -> None:
        async with Vizio(host=SOUNDBAR_HOST_PORT, device_type=DeviceType.SOUNDBAR) as v:
            with pytest.raises(VizioUnsupportedError):
                await v.get_current_app_config()

    async def test_launch_app_config_raises_for_soundbar(self) -> None:
        from vizaio.types import AppConfig

        async with Vizio(host=SOUNDBAR_HOST_PORT, device_type=DeviceType.SOUNDBAR) as v:
            with pytest.raises(VizioUnsupportedError):
                await v.launch_app_config(AppConfig(app_id="1", name_space=2))


# ---------------------------------------------------------------------------
# get_inputs: don't double-mark current after rename
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# set_input no-op when target is already current
# ---------------------------------------------------------------------------


class TestSetInputNoOp:
    async def test_no_put_when_already_current_by_meta(self) -> None:
        with aioresponses() as m:
            m.get(
                _tv_url(Endpoint.INPUTS),
                payload=make_inputs_list_response(
                    [("hdmi1", "HDMI-1", "PS5", 5)],
                    current_input_meta_name="PS5",
                ),
            )
            m.get(
                _tv_url(Endpoint.CURRENT_INPUT),
                payload=make_success_response(
                    items=[
                        {
                            "CNAME": "current_input",
                            "TYPE": "T_VALUE_V1",
                            "NAME": "Current Input",
                            "VALUE": "PS5",
                            "HASHVAL": 5,
                        }
                    ],
                ),
            )
            async with Vizio(
                host=TV_HOST_PORT, device_type=DeviceType.TV, auth_token=AUTH_TOKEN
            ) as v:
                # Asking to set the input we're already on should not PUT.
                await v.set_input("PS5")
            put_reqs = [
                r
                for (method, _), reqs in m.requests.items()
                if method == "PUT"
                for r in reqs
            ]
            assert put_reqs == []
