"""Endpoint catalog: path builders and per-profile resolver.

Two layers under test:

1. **Path builders** (``settings_path``, ``state_path``, ``pairing_path``):
   pure functions that construct URL paths. Easy to verify against
   pyvizio's known-good string constants.

2. **Resolver** (``resolve(endpoint, profile)`` returning ``EndpointSpec``):
   produces the right paths/method/auth for a given (endpoint, profile)
   pair. Surfaces unsupported endpoints (e.g., ``LAUNCH_APP`` on a
   soundbar) by raising ``VizioUnsupportedError``.

The resolver implementation lives in ``_endpoints.py`` (TODO until #27).
These tests fail until then.
"""

from __future__ import annotations

import pytest

from vizaio import (
    AuthRequirement,
    DeviceType,
    VizioUnsupportedError,
)
from vizaio.endpoints import (
    Endpoint,
    EndpointSpec,
    SettingsRoot,
    pairing_path,
    settings_options_path,
    settings_path,
    state_path,
)


class TestPathBuilders:
    """Compositional path construction matches the URLs pyvizio uses."""

    def test_state_path_simple(self) -> None:
        assert state_path("device", "power_mode") == "/state/device/power_mode"

    def test_state_path_single_segment(self) -> None:
        assert state_path("device") == "/state/device"

    def test_settings_path_root(self) -> None:
        # Root listing — no parts after the settings root.
        assert settings_path(SettingsRoot.TV) == "/menu_native/dynamic/tv_settings"
        assert (
            settings_path(SettingsRoot.AUDIO) == "/menu_native/dynamic/audio_settings"
        )

    def test_settings_path_nested(self) -> None:
        assert (
            settings_path(SettingsRoot.TV, "audio", "volume")
            == "/menu_native/dynamic/tv_settings/audio/volume"
        )

    def test_settings_path_deep(self) -> None:
        # ESN lives 5 segments deep — pyvizio's exact path.
        expected = (
            "/menu_native/dynamic/tv_settings/admin_and_privacy/"
            "system_information/tv_information/esn"
        )
        actual = settings_path(
            SettingsRoot.TV,
            "admin_and_privacy",
            "system_information",
            "tv_information",
            "esn",
        )
        assert actual == expected

    def test_settings_options_path(self) -> None:
        assert (
            settings_options_path(SettingsRoot.TV, "audio")
            == "/menu_native/static/tv_settings/audio"
        )

    def test_pairing_paths(self) -> None:
        assert pairing_path("start") == "/pairing/start"
        assert pairing_path("pair") == "/pairing/pair"
        assert pairing_path("cancel") == "/pairing/cancel"


class TestResolveCommon:
    """Endpoints that exist on every device type resolve consistently."""

    @pytest.mark.parametrize("dtype", list(DeviceType))
    def test_power_mode(self, dtype: DeviceType) -> None:
        spec = _resolve(Endpoint.POWER_MODE, dtype)
        assert spec.method == "GET"
        assert spec.paths == ("/state/device/power_mode",)
        assert spec.item_cname == "power_mode"

    @pytest.mark.parametrize("dtype", list(DeviceType))
    def test_device_info(self, dtype: DeviceType) -> None:
        spec = _resolve(Endpoint.DEVICE_INFO, dtype)
        assert spec.method == "GET"
        assert spec.paths == ("/state/device/deviceinfo",)
        # Always unauthed — even auth-requiring TVs allow this for
        # connection probes (used by `ping()`).
        assert spec.auth is AuthRequirement.NONE

    @pytest.mark.parametrize("dtype", list(DeviceType))
    def test_key_press(self, dtype: DeviceType) -> None:
        spec = _resolve(Endpoint.KEY_PRESS, dtype)
        assert spec.method == "PUT"
        assert spec.paths == ("/key_command/",)


class TestResolveAuthRequirements:
    """Auth requirement varies by device family — see protocol-notes #18."""

    def test_tv_settings_required(self) -> None:
        spec = _resolve(Endpoint.SETTINGS, DeviceType.TV)
        assert spec.auth is AuthRequirement.REQUIRED

    def test_soundbar_settings_optional_or_none(self) -> None:
        spec = _resolve(Endpoint.SETTINGS, DeviceType.SOUNDBAR)
        # Soundbars don't require auth; either NONE or OPTIONAL is correct.
        assert spec.auth in (AuthRequirement.NONE, AuthRequirement.OPTIONAL)

    def test_crave_settings_optional_or_none(self) -> None:
        spec = _resolve(Endpoint.SETTINGS, DeviceType.CRAVE360)
        assert spec.auth in (AuthRequirement.NONE, AuthRequirement.OPTIONAL)

    def test_pairing_endpoints_unauthenticated(self) -> None:
        # Pairing happens before we have a token.
        for endpoint in (
            Endpoint.BEGIN_PAIR,
            Endpoint.FINISH_PAIR,
            Endpoint.CANCEL_PAIR,
        ):
            spec = _resolve(endpoint, DeviceType.TV)
            assert spec.auth is AuthRequirement.NONE, (
                f"{endpoint} should not require auth"
            )


class TestResolveSettingsTreePaths:
    """Settings paths differ between TV (tv_settings) and audio devices
    (audio_settings) — protocol-notes #1, #3."""

    def test_tv_uses_tv_settings(self) -> None:
        spec = _resolve(Endpoint.SETTINGS, DeviceType.TV)
        assert all("tv_settings" in p for p in spec.paths)

    def test_soundbar_uses_audio_settings(self) -> None:
        spec = _resolve(Endpoint.SETTINGS, DeviceType.SOUNDBAR)
        assert all("audio_settings" in p for p in spec.paths)

    def test_crave_uses_audio_settings(self) -> None:
        spec = _resolve(Endpoint.SETTINGS, DeviceType.CRAVE360)
        assert all("audio_settings" in p for p in spec.paths)


class TestResolveFirmwareFallbacks:
    """ESN/serial/version live at multiple paths across firmware versions —
    protocol-notes #3, pyvizio open issue #135."""

    def test_esn_has_multiple_paths(self) -> None:
        spec = _resolve(Endpoint.ESN, DeviceType.TV)
        assert len(spec.paths) >= 2, (
            "ESN must have primary + at least one fallback path"
        )

    def test_serial_has_multiple_paths(self) -> None:
        spec = _resolve(Endpoint.SERIAL_NUMBER, DeviceType.TV)
        assert len(spec.paths) >= 2

    def test_version_has_multiple_paths(self) -> None:
        spec = _resolve(Endpoint.VERSION, DeviceType.TV)
        assert len(spec.paths) >= 2

    def test_paths_distinct(self) -> None:
        spec = _resolve(Endpoint.ESN, DeviceType.TV)
        assert len(set(spec.paths)) == len(spec.paths)


class TestResolveUnsupported:
    """Unsupported endpoint+profile combinations raise VizioUnsupportedError
    BEFORE any HTTP request goes out — fast-fail rather than 404."""

    def test_battery_on_tv_unsupported(self) -> None:
        with pytest.raises(VizioUnsupportedError):
            _resolve(Endpoint.BATTERY_LEVEL, DeviceType.TV)

    def test_battery_on_soundbar_unsupported(self) -> None:
        with pytest.raises(VizioUnsupportedError):
            _resolve(Endpoint.BATTERY_LEVEL, DeviceType.SOUNDBAR)

    @pytest.mark.parametrize(
        "dtype",
        [DeviceType.CRAVE_GO, DeviceType.CRAVE360, DeviceType.CRAVE_PRO],
    )
    def test_battery_on_crave_supported(self, dtype: DeviceType) -> None:
        # All Crave variants have batteries.
        spec = _resolve(Endpoint.BATTERY_LEVEL, dtype)
        assert spec.method == "GET"
        # Per APK findings, the path is /state/device/battery_level —
        # direct state endpoint, not buried in the menu tree.
        assert spec.paths == ("/state/device/battery_level",)

    def test_charging_on_tv_unsupported(self) -> None:
        with pytest.raises(VizioUnsupportedError):
            _resolve(Endpoint.CHARGING_STATUS, DeviceType.TV)

    def test_apps_on_soundbar_unsupported(self) -> None:
        with pytest.raises(VizioUnsupportedError):
            _resolve(Endpoint.LAUNCH_APP, DeviceType.SOUNDBAR)

    def test_apps_on_crave_unsupported(self) -> None:
        with pytest.raises(VizioUnsupportedError):
            _resolve(Endpoint.CURRENT_APP, DeviceType.CRAVE360)

    def test_inputs_on_soundbar_unsupported(self) -> None:
        with pytest.raises(VizioUnsupportedError):
            _resolve(Endpoint.INPUTS, DeviceType.SOUNDBAR)


class TestStateExtended:
    """`/state_extended` is a bulk-state endpoint that returns multiple
    state values in one round trip — used by the official app and worth
    exposing for HA polling efficiency."""

    def test_state_extended_path(self) -> None:
        spec = _resolve(Endpoint.STATE_EXTENDED, DeviceType.TV)
        assert spec.paths == ("/state_extended",)
        assert spec.method == "GET"

    def test_state_extended_authed(self) -> None:
        # Per APK findings, /state_extended requires AUTH.
        spec = _resolve(Endpoint.STATE_EXTENDED, DeviceType.TV)
        assert spec.auth is AuthRequirement.REQUIRED


class TestEndpointSpecImmutability:
    """EndpointSpec is frozen — callers can stash it without worrying about
    mutation surprises across requests."""

    def test_frozen(self) -> None:
        spec = _resolve(Endpoint.POWER_MODE, DeviceType.TV)
        with pytest.raises((AttributeError, TypeError)):
            spec.method = "POST"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Helper — keeps test code clean and centralizes the (endpoint, profile)
# resolution call. The actual `resolve()` function is implemented as part
# of #27. Until then, this helper imports it lazily so collection works
# even if the implementation isn't there yet.
# ---------------------------------------------------------------------------


def _resolve(endpoint: Endpoint, dtype: DeviceType) -> EndpointSpec:
    """Resolve an endpoint for a device type, going through the profile."""
    from vizaio.endpoints import resolve

    return resolve(endpoint, dtype.profile)
