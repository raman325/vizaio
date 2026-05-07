"""Coverage for ``parse`` module's defensive degrade paths.

These tests target the malformed-payload branches: shapes that real
firmware emits inconsistently across versions, or that future firmware
might emit. Each ensures the parser degrades to a typed empty default
rather than raising — important for poll-loop callers that don't want
a single odd field to abort an entire refresh.
"""

from __future__ import annotations

import pytest

from vizaio.errors import (
    VizioNotFoundError,
    VizioResponseError,
)
from vizaio.parse import (
    _bool_or_default,
    _coerce_setting_type,
    _input_meta_name,
    parse_auth_token,
    parse_current_app_config,
    parse_current_input,
    parse_device_info,
    parse_inputs,
    parse_model_name,
    parse_pair_challenge,
    parse_setting_types,
    parse_system_info_model_name,
)
from vizaio.types import SettingType
from vizaio.wire import Item, Response


def _resp(payload: dict) -> Response:
    return Response.from_json(payload)


# ---------------------------------------------------------------------------
# parse_inputs — current_input as Mapping (nested 'name')
# ---------------------------------------------------------------------------


class TestParseInputsCurrentItemShape:
    def test_current_input_as_mapping_with_name(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEMS": [
                    {
                        "CNAME": "hdmi1",
                        "TYPE": "T_DEVICE_V1",
                        "NAME": "HDMI-1",
                        "VALUE": {"NAME": "PS5", "METADATA": ""},
                        "HASHVAL": 1,
                    },
                    {
                        "CNAME": "current_input",
                        "TYPE": "T_VALUE_V1",
                        "NAME": "Current Input",
                        # Nested {"name": ...} form.
                        "VALUE": {"NAME": "PS5"},
                    },
                ],
            }
        )
        inputs = parse_inputs(resp, current_input_name=None)
        assert any(i.is_current and i.name == "HDMI-1" for i in inputs)

    def test_current_input_with_unexpected_shape_ignored(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEMS": [
                    {
                        "CNAME": "hdmi1",
                        "TYPE": "T_DEVICE_V1",
                        "NAME": "HDMI-1",
                        "VALUE": {"NAME": "PS5"},
                        "HASHVAL": 1,
                    },
                    {
                        "CNAME": "current_input",
                        "TYPE": "T_VALUE_V1",
                        "NAME": "Current Input",
                        "VALUE": 42,  # int, neither str nor Mapping
                    },
                ],
            }
        )
        # Doesn't raise; no input is marked current.
        inputs = parse_inputs(resp, current_input_name=None)
        assert len(inputs) == 1
        assert inputs[0].is_current is False


# ---------------------------------------------------------------------------
# parse_current_input
# ---------------------------------------------------------------------------


class TestParseCurrentInput:
    def test_value_as_mapping_with_name(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEMS": [
                    {
                        "CNAME": "current_input",
                        "TYPE": "T_VALUE_V1",
                        "NAME": "Current Input",
                        "VALUE": {"NAME": "HDMI-2"},
                    }
                ],
            }
        )
        assert parse_current_input(resp) == "HDMI-2"

    def test_unexpected_value_shape_raises(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEMS": [
                    {
                        "CNAME": "current_input",
                        "TYPE": "T_VALUE_V1",
                        "NAME": "Current Input",
                        "VALUE": [1, 2, 3],  # list, not str or Mapping
                    }
                ],
            }
        )
        with pytest.raises(VizioResponseError, match="VALUE shape"):
            parse_current_input(resp)


# ---------------------------------------------------------------------------
# parse_current_app_config
# ---------------------------------------------------------------------------


class TestParseCurrentAppConfig:
    def test_no_items_returns_none(self) -> None:
        resp = _resp({"STATUS": {"RESULT": "SUCCESS"}, "ITEMS": []})
        assert parse_current_app_config(resp) is None

    def test_value_null_returns_none(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEM": {"VALUE": None},
            }
        )
        assert parse_current_app_config(resp) is None

    def test_value_non_mapping_returns_none(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEM": {"VALUE": "stringy"},
            }
        )
        assert parse_current_app_config(resp) is None

    def test_partial_value_with_nones_returns_none(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEM": {"VALUE": {"APP_ID": None, "NAME_SPACE": None}},
            }
        )
        assert parse_current_app_config(resp) is None


# ---------------------------------------------------------------------------
# parse_pair_challenge
# ---------------------------------------------------------------------------


class TestParsePairChallenge:
    def test_no_items_raises_not_found(self) -> None:
        resp = _resp({"STATUS": {"RESULT": "SUCCESS"}, "ITEMS": []})
        with pytest.raises(VizioNotFoundError, match="begin_pair"):
            parse_pair_challenge(resp)

    def test_missing_token_raises(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEM": {"CHALLENGE_TYPE": 1},  # missing PAIRING_REQ_TOKEN
            }
        )
        with pytest.raises(VizioResponseError, match="PAIRING_REQ_TOKEN"):
            parse_pair_challenge(resp)


# ---------------------------------------------------------------------------
# parse_auth_token
# ---------------------------------------------------------------------------


class TestParseAuthToken:
    def test_no_items_raises_not_found(self) -> None:
        resp = _resp({"STATUS": {"RESULT": "SUCCESS"}, "ITEMS": []})
        with pytest.raises(VizioNotFoundError, match="finish_pair"):
            parse_auth_token(resp)

    def test_missing_token_raises(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEM": {"AUTH_TOKEN": 12345},  # not a string
            }
        )
        with pytest.raises(VizioResponseError, match="AUTH_TOKEN"):
            parse_auth_token(resp)


# ---------------------------------------------------------------------------
# parse_device_info
# ---------------------------------------------------------------------------


class TestParseDeviceInfo:
    def test_no_items_returns_empty(self) -> None:
        resp = _resp({"STATUS": {"RESULT": "SUCCESS"}, "ITEMS": []})
        assert parse_device_info(resp) == {}

    def test_value_not_mapping_returns_empty(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEMS": [
                    {
                        "CNAME": "info",
                        "TYPE": "T_VALUE_V1",
                        "NAME": "info",
                        "VALUE": "scalar",
                    }
                ],
            }
        )
        assert parse_device_info(resp) == {}


class TestParseModelName:
    def test_soundbar_uses_name_field(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEMS": [
                    {
                        "CNAME": "info",
                        "TYPE": "T_VALUE_V1",
                        "NAME": "info",
                        "VALUE": {"NAME": "SB3651"},
                    }
                ],
            }
        )
        assert parse_model_name(resp, settings_root="audio_settings") == "SB3651"

    def test_tv_returns_empty_when_field_missing(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEMS": [
                    {
                        "CNAME": "info",
                        "TYPE": "T_VALUE_V1",
                        "NAME": "info",
                        "VALUE": {"OTHER": "x"},
                    }
                ],
            }
        )
        assert parse_model_name(resp, settings_root="tv_settings") == ""


# ---------------------------------------------------------------------------
# parse_setting_types
# ---------------------------------------------------------------------------


class TestParseSettingTypes:
    def test_returns_cnames_for_menu_items(self) -> None:
        resp = _resp(
            {
                "STATUS": {"RESULT": "SUCCESS"},
                "ITEMS": [
                    {
                        "CNAME": "audio",
                        "TYPE": "T_MENU_V1",
                        "NAME": "Audio",
                        "VALUE": "",
                    },
                    {
                        "CNAME": "picture",
                        "TYPE": "T_MENU_V1",
                        "NAME": "Picture",
                        "VALUE": "",
                    },
                ],
            }
        )
        assert parse_setting_types(resp) == ["audio", "picture"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestBoolOrDefault:
    def test_none_uses_default(self) -> None:
        assert _bool_or_default(None, default=True) is True
        assert _bool_or_default(None, default=False) is False

    def test_bool_passthrough(self) -> None:
        assert _bool_or_default(True, default=False) is True
        assert _bool_or_default(False, default=True) is False

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("true", True),
            ("YES", True),
            ("on", True),
            ("1", True),
            ("false", False),
            ("no", False),
            ("0", False),
            ("nonsense", False),
        ],
    )
    def test_strings(self, value: str, expected: bool) -> None:
        assert _bool_or_default(value, default=False) is expected

    def test_int_truthy_falsy(self) -> None:
        assert _bool_or_default(1, default=False) is True
        assert _bool_or_default(0, default=True) is False

    def test_unknown_type_uses_default(self) -> None:
        # A list isn't None/bool/str/numeric → default.
        assert _bool_or_default([1, 2], default=True) is True


class TestInputMetaName:
    def test_string_value(self) -> None:
        item = Item(
            cname="hdmi1",
            type="T_DEVICE_V1",
            name="HDMI-1",
            value="PS5",
            hashval=1,
            raw={},
        )
        assert _input_meta_name(item) == "PS5"

    def test_mapping_with_name(self) -> None:
        item = Item(
            cname="hdmi1",
            type="T_DEVICE_V1",
            name="HDMI-1",
            value={"name": "PS5"},
            hashval=1,
            raw={},
        )
        assert _input_meta_name(item) == "PS5"

    def test_mapping_without_name(self) -> None:
        item = Item(
            cname="hdmi1",
            type="T_DEVICE_V1",
            name="HDMI-1",
            value={"metadata": ""},
            hashval=1,
            raw={},
        )
        assert _input_meta_name(item) == ""

    def test_unknown_value_returns_empty(self) -> None:
        item = Item(
            cname="hdmi1",
            type="T_DEVICE_V1",
            name="HDMI-1",
            value=42,
            hashval=1,
            raw={},
        )
        assert _input_meta_name(item) == ""


class TestCoerceSettingType:
    def test_known_type(self) -> None:
        assert _coerce_setting_type("T_VALUE_ABS_V1") is SettingType.SLIDER

    def test_unknown_falls_back_to_menu(self) -> None:
        assert _coerce_setting_type("T_FUTURE_TYPE") is SettingType.MENU


# ---------------------------------------------------------------------------
# parse_system_info_model_name
# ---------------------------------------------------------------------------


class TestParseSystemInfoModelName:
    """Extracts ``SYSTEM_INFO.MODEL_NAME`` from a deviceinfo response.
    This is the canonical model identifier (e.g., ``"VHD24M-0810"``,
    ``"SP30-E0"``) — distinct from the friendly ``NAME`` field that
    ``parse_model_name`` returns for non-TV settings roots."""

    def test_returns_model_name_from_live_capture(
        self, deviceinfo_response: Response
    ) -> None:
        # Live VHD24M-0810 capture, verified at SYSTEM_INFO.MODEL_NAME.
        assert parse_system_info_model_name(deviceinfo_response) == "VHD24M-0810"

    def test_returns_empty_when_response_has_no_items(self) -> None:
        empty = Response.from_json({"ITEMS": [], "STATUS": {"RESULT": "SUCCESS"}})
        assert parse_system_info_model_name(empty) == ""

    def test_returns_empty_when_system_info_missing(self) -> None:
        no_system = Response.from_json(
            {
                "ITEMS": [{"VALUE": {"MODEL_NAME": "x"}, "CNAME": "deviceinfo"}],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        assert parse_system_info_model_name(no_system) == ""

    def test_returns_empty_when_model_name_missing(self) -> None:
        no_model = Response.from_json(
            {
                "ITEMS": [
                    {"VALUE": {"SYSTEM_INFO": {"CHIPSET": 4}}, "CNAME": "deviceinfo"}
                ],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        assert parse_system_info_model_name(no_model) == ""
