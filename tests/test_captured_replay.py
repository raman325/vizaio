"""
Fixture replay — assert the library produces correct typed output from the
real-device payloads captured in ``tests/captured/``.

These fixtures were captured live from a VHD24M-0810 (firmware 3.720.9.1-1)
during a hardware-verification pass. They lock in the on-the-wire shapes so
parser changes can't silently break what the device actually sends.

Why this exists separately from the unit-mocked tests:

- The unit tests construct synthetic envelopes via ``make_*_response``
  helpers. Those helpers reflect *our model* of the protocol, which is
  itself derived from APK research and may not match every firmware.
- The captured fixtures are ground truth for one specific firmware.
  If a parser change breaks against a captured fixture, that's a real
  regression; if it breaks against a synthetic helper but not the
  fixture, the helper has drifted from reality and needs an update.
"""

from __future__ import annotations

import json
from pathlib import Path

from vizio_smartcast.errors import (
    VizioInvalidParameterError,
    VizioNotFoundError,
)
from vizio_smartcast.parse import (
    parse_current_app_config,
    parse_current_input,
    parse_inputs,
    parse_setting,
    parse_setting_types,
    parse_settings,
    parse_state_extended,
)
from vizio_smartcast.types import ResponseStatus, SettingType
from vizio_smartcast.wire import Response

CAPTURED = Path(__file__).parent / "captured"


def _load(name: str) -> dict:
    return json.loads((CAPTURED / f"{name}.json").read_text())


# ---------------------------------------------------------------------------
# Status / envelope parsing
# ---------------------------------------------------------------------------


class TestEnvelopeStatusParsing:
    """Each captured response's STATUS round-trips through Response.from_json."""

    def test_uri_not_found_status(self) -> None:
        """Modern firmware emits URI_NOT_FOUND for paths it doesn't expose."""
        response = Response.from_json(_load("esn_modern_404"))
        assert response.status is ResponseStatus.URI_NOT_FOUND
        assert response.detail == "URI not found"

    def test_legacy_tv_information_path_404(self) -> None:
        response = Response.from_json(_load("tv_information_legacy_404"))
        assert response.status is ResponseStatus.URI_NOT_FOUND

    def test_event_register_invalid_parameter(self) -> None:
        """The originally-documented EVENT_REGISTER_BODY of {REQUEST: MODIFY}
        returns INVALID_PARAMETER on this firmware. Captured to lock in
        the call shape that proves we need VALUE: TRUE."""
        response = Response.from_json(_load("event_register_unsup"))
        assert response.status is ResponseStatus.INVALID_PARAMETER

    def test_success_responses_round_trip(self) -> None:
        """Every other captured fixture is a SUCCESS response."""
        for name in [
            "device_info",
            "power_mode",
            "inputs",
            "current_input",
            "current_app",
            "settings_root",
            "settings_audio",
            "settings_audio_volume",
            "settings_audio_mute",
            "settings_audio_dialogue_enhancer",
            "settings_audio_lip_sync",
            "static_audio",
            "tv_information_modern",
        ]:
            response = Response.from_json(_load(name))
            assert response.status is ResponseStatus.SUCCESS, (
                f"{name} expected SUCCESS, got {response.status}"
            )


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class TestCapturedInputs:
    """The captured device has 5 inputs: cast, hdmi1, hdmi2, comp, tuner.
    HDMI-2 has been renamed to 'Mac' (user rename). All inputs reported
    enabled=FALSE at capture time. No synthetic current_input item is
    present — modern firmware doesn't include it."""

    def test_inputs_parsed_with_cname_and_meta(self) -> None:
        response = Response.from_json(_load("inputs"))
        # Without current_input_name hint, is_current is all False.
        inputs = parse_inputs(response)
        assert len(inputs) == 5
        by_cname = {i.cname: i for i in inputs}
        assert set(by_cname) == {"cast", "hdmi1", "hdmi2", "comp", "tuner"}
        # CAST has divergent display name vs meta_name even at factory
        # default — the meta_name 'SMARTCAST' is set by the firmware, not
        # by user rename. This is the canonical example of why our
        # set_input/is_current logic must accept either form.
        assert by_cname["cast"].name == "CAST"
        assert by_cname["cast"].meta_name == "SMARTCAST"
        # Other inputs at capture time had not been user-renamed, so
        # name == meta_name. The library must still resolve all four
        # forms (cname/name/meta_name/case-insensitive) for set_input.
        assert by_cname["hdmi2"].name == "HDMI-2"
        assert by_cname["hdmi2"].meta_name == "HDMI-2"

    def test_is_current_resolves_via_separate_current_input(self) -> None:
        """When the inputs response has no synthetic current_input,
        the caller (Vizio.get_inputs) must pass current_input_name from
        a separate fetch. Captured current_input.value was 'SMARTCAST',
        so the cast input should be marked current."""
        inputs_response = Response.from_json(_load("inputs"))
        current_response = Response.from_json(_load("current_input"))
        current_meta_name = parse_current_input(current_response)
        assert current_meta_name == "SMARTCAST"
        inputs = parse_inputs(inputs_response, current_input_name=current_meta_name)
        currents = [i for i in inputs if i.is_current]
        assert len(currents) == 1
        assert currents[0].cname == "cast"


# ---------------------------------------------------------------------------
# Current app / current input
# ---------------------------------------------------------------------------


class TestCapturedCurrentApp:
    """Captured device was on SmartCast Home: app_id='1', name_space=4,
    message=JSON-state payload."""

    def test_current_app_config_extracted(self) -> None:
        response = Response.from_json(_load("current_app"))
        cfg = parse_current_app_config(response)
        assert cfg is not None
        assert cfg.app_id == "1"
        assert cfg.name_space == 4
        # Real devices send a JSON-state message, not a URL like
        # APP_HOME's hardcoded value implies.
        assert cfg.message is not None
        assert "home" in cfg.message


class TestCapturedCurrentInput:
    def test_current_input_value_is_meta_name_for_cast(self) -> None:
        """For the cast input, current_input.VALUE = 'SMARTCAST'
        (the meta_name). Different from HDMI inputs which return the
        display name. Inconsistency is firmware behavior, not our bug."""
        response = Response.from_json(_load("current_input"))
        assert parse_current_input(response) == "SMARTCAST"


# ---------------------------------------------------------------------------
# Setting types & individual settings
# ---------------------------------------------------------------------------


class TestCapturedSettingTypes:
    """Captured setting_types: picture, audio, network, channels,
    accessibility, devices, system, admin_and_privacy, cast.
    Validates protocol-notes #7: pyvizio's filter for cast/input/devices/
    network was a pyvizio invention. None of these should be filtered out."""

    def test_all_categories_returned(self) -> None:
        response = Response.from_json(_load("settings_root"))
        types = parse_setting_types(response)
        # Captured exactly: picture, audio, network, channels, accessibility,
        # devices, system, admin_and_privacy, cast.
        expected = {
            "picture",
            "audio",
            "network",
            "channels",
            "accessibility",
            "devices",
            "system",
            "admin_and_privacy",
            "cast",
        }
        assert set(types) == expected, (
            f"missing or unexpected types: got={set(types)} expected={expected}"
        )


class TestCapturedSettingsLeaves:
    """Individual setting leaves with options merged from the static tree."""

    def test_dialogue_enhancer_options_merged(self) -> None:
        """T_LIST_V1 with elements ('Off', 'Low', 'Medium', 'High')."""
        values = Response.from_json(_load("settings_audio_dialogue_enhancer"))
        # Static options live in static_audio (parent endpoint here);
        # this also exercises the merge logic from parse_settings.
        options = Response.from_json(_load("static_audio"))
        merged = parse_settings(values, options, setting_type="audio")
        assert "dialogue_enhancer" in merged
        info = merged["dialogue_enhancer"]
        assert info.type is SettingType.LIST
        assert info.options == ("Off", "Low", "Medium", "High")
        assert info.value == "Off"
        assert info.hashval == 4284435521

    def test_lip_sync_bounds_merged(self) -> None:
        """T_VALUE_V1 with min=0, max=20 — bounds come from static tree."""
        values = Response.from_json(_load("settings_audio_lip_sync"))
        options = Response.from_json(_load("static_audio"))
        merged = parse_settings(values, options, setting_type="audio")
        assert "lip_sync" in merged
        info = merged["lip_sync"]
        assert info.type is SettingType.INT
        assert info.value == 0
        # Bounds-validated against captured static_audio.json.
        assert info.min == 0
        assert info.max == 20

    def test_volume_value(self) -> None:
        """Volume parses to int from a real T_VALUE_V1 leaf.

        The exact integer captured here is a snapshot of device state,
        not a contract — what matters for a regression test is type
        and parseability.
        """
        response = Response.from_json(_load("settings_audio_volume"))
        item = response.require_item("volume")
        info = parse_setting(item, setting_type="audio")
        assert isinstance(info.value, int)
        assert info.type is SettingType.INT


# ---------------------------------------------------------------------------
# tv_information aggregate (the modern firmware identity endpoint)
# ---------------------------------------------------------------------------


class TestCapturedTvInformationAggregate:
    """Modern firmware returns all identity fields in one envelope."""

    def test_aggregate_contains_serial_firmware_model(self) -> None:
        response = Response.from_json(_load("tv_information_modern"))
        cnames = {item.cname: item.value for item in response.items}
        # PII-scrubbed values; original captured response had real values.
        assert cnames.get("serial_number") == "TEST00000000001"
        # Model number is hardware identification, not user-identifying.
        assert cnames.get("model_name") == "VHD24M-0810"
        # Modern firmware exposes 'firmware', not 'version'.
        assert cnames.get("firmware") == "3.720.9.1-1"
        # ESN does NOT appear in this firmware's aggregate — important
        # for the Vizio._identity_field "" graceful-degrade path.
        assert "esn" not in cnames

    def test_legacy_path_404s(self) -> None:
        """Legacy tv_information path returns URI_NOT_FOUND on this firmware
        — the multi-path endpoint resolver must trigger fallback on it."""
        response = Response.from_json(_load("tv_information_legacy_404"))
        assert response.status is ResponseStatus.URI_NOT_FOUND


# ---------------------------------------------------------------------------
# state_extended (non-standard envelope)
# ---------------------------------------------------------------------------


class TestCapturedStateExtended:
    """state_extended uses flat top-level keys, no STATUS/ITEMS wrapper.
    parse_state_extended consumes the raw payload directly."""

    def test_full_payload_round_trip(self) -> None:
        payload = _load("state_extended")
        s = parse_state_extended(payload)
        assert s.power_on is True
        assert s.power_mode == "Quick Start"
        assert s.current_input == "SMARTCAST"
        assert s.current_input_hashval == 3009117460
        assert s.current_app is not None
        assert s.current_app.app_id == "1"
        assert s.current_app.name_space == 4
        assert s.screen_mode == "Full screen"
        assert s.media_state == "MediaState::Stopped"
        # PII-scrubbed; original device_name carried the user's TV name.
        assert s.device_name == "Test TV"
        assert s.errors == ()


# ---------------------------------------------------------------------------
# Power mode
# ---------------------------------------------------------------------------


class TestCapturedPowerMode:
    def test_power_mode_value(self) -> None:
        response = Response.from_json(_load("power_mode"))
        item = response.require_item("power_mode")
        # Captured while powered on — value was 1 (truthy).
        assert bool(item.value) is True


# ---------------------------------------------------------------------------
# Status mapping under client semantics
# ---------------------------------------------------------------------------


class TestStatusToExceptionMapping:
    """The exception mapping in client._check_status must produce the
    same exception types from captured-fixture statuses as it would
    from synthetic ones. This catches drift between client.py logic
    and types.ResponseStatus enum."""

    def test_uri_not_found_maps_to_not_found_error(self) -> None:
        from vizio_smartcast.client import _check_status
        from vizio_smartcast.endpoints import EndpointSpec
        from vizio_smartcast.types import AuthRequirement

        response = Response.from_json(_load("esn_modern_404"))
        spec = EndpointSpec(
            paths=("/x",),
            method="GET",
            auth=AuthRequirement.REQUIRED,
            item_cname=None,
        )
        try:
            _check_status(response, spec)
        except VizioNotFoundError:
            return
        raise AssertionError("expected VizioNotFoundError")

    def test_invalid_parameter_maps_to_invalid_parameter_error(self) -> None:
        from vizio_smartcast.client import _check_status
        from vizio_smartcast.endpoints import EndpointSpec
        from vizio_smartcast.types import AuthRequirement

        response = Response.from_json(_load("event_register_unsup"))
        spec = EndpointSpec(
            paths=("/event/register",),
            method="PUT",
            auth=AuthRequirement.REQUIRED,
            item_cname=None,
        )
        try:
            _check_status(response, spec)
        except VizioInvalidParameterError:
            return
        raise AssertionError("expected VizioInvalidParameterError")
