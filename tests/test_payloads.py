"""PUT body builders.

Centralizes every PUT payload construction. Tests assert the exact wire
format pyvizio uses, because those are the shapes proven to work against
real devices.

The builders live in ``vizaio._payloads``.

Important: these tests assert the *exact* dict shape, including key
casing. The device requires uppercase request keys (``HASHVAL``, ``VALUE``,
``REQUEST``) — see protocol-notes.md quirk on outbound payloads. Lowering
keys would break writes silently.
"""

from __future__ import annotations

import pytest

from vizaio import AppConfig
from vizaio._payloads import (
    begin_pair,
    cancel_pair,
    finish_pair,
    key_press,
    launch_app,
    set_input,
    write_setting,
)


class TestWriteSetting:
    """The basic setting-write payload: VALUE + HASHVAL + REQUEST=MODIFY."""

    def test_int_value(self) -> None:
        body = write_setting(value=25, hashval=12345)
        assert body == {"VALUE": 25, "HASHVAL": 12345, "REQUEST": "MODIFY"}

    def test_str_value(self) -> None:
        body = write_setting(value="On", hashval=999)
        assert body == {"VALUE": "On", "HASHVAL": 999, "REQUEST": "MODIFY"}

    def test_zero_hashval(self) -> None:
        # Hashval 0 is a real value the device may return; must round-trip
        # cleanly through the builder.
        body = write_setting(value=0, hashval=0)
        assert body["HASHVAL"] == 0

    def test_keys_are_uppercase(self) -> None:
        """Device requires uppercase keys on writes — protocol notes."""
        body = write_setting(value=1, hashval=1)
        for key in body:
            assert key.isupper(), f"{key!r} is not uppercase"


class TestKeyPress:
    """KEYLIST payload — protocol-notes quirk #19."""

    def test_single_key(self) -> None:
        body = key_press([(11, 1)])  # POW_ON
        assert body == {"KEYLIST": [{"CODESET": 11, "CODE": 1, "ACTION": "KEYPRESS"}]}

    def test_multiple_distinct_keys(self) -> None:
        # Sequence: VOL_UP three times.
        body = key_press([(5, 1), (5, 1), (5, 1)])
        assert len(body["KEYLIST"]) == 3
        for entry in body["KEYLIST"]:
            assert entry == {"CODESET": 5, "CODE": 1, "ACTION": "KEYPRESS"}

    def test_codeset_zero_allowed(self) -> None:
        # CODESET 0 / CODE 0 may be valid on some keys — don't reject.
        body = key_press([(0, 0)])
        assert body["KEYLIST"][0]["CODESET"] == 0
        assert body["KEYLIST"][0]["CODE"] == 0

    def test_action_always_keypress(self) -> None:
        """Per protocol-notes #19, only KEYPRESS is supported. Builder
        should hardcode it — no caller-supplied action."""
        body = key_press([(11, 1), (5, 1)])
        for entry in body["KEYLIST"]:
            assert entry["ACTION"] == "KEYPRESS"

    def test_empty_keylist_rejected(self) -> None:
        # Sending KEYLIST: [] would be a no-op at best, error at worst.
        # Force callers to handle this themselves.
        with pytest.raises(ValueError):
            key_press([])

    def test_large_keylist_no_builder_side_chunking(self) -> None:
        """Per APK findings: the official app sends arbitrary-length
        KEYLISTs in one PUT, no client-side chunking. The payload builder
        just builds — defensive chunking (if any) is Vizio.volume_up's
        concern."""
        body = key_press([(5, 1)] * 100)
        assert len(body["KEYLIST"]) == 100


class TestPairing:
    """begin_pair / finish_pair / cancel_pair payloads."""

    def test_begin_pair(self) -> None:
        body = begin_pair(device_id="ha-coord", device_name="HomeAssistant")
        assert body == {"DEVICE_ID": "ha-coord", "DEVICE_NAME": "HomeAssistant"}

    def test_finish_pair(self) -> None:
        body = finish_pair(
            device_id="ha-coord", challenge_type=1, token=54321, pin="1234"
        )
        assert body == {
            "DEVICE_ID": "ha-coord",
            "CHALLENGE_TYPE": 1,
            "PAIRING_REQ_TOKEN": 54321,
            "RESPONSE_VALUE": "1234",
        }

    def test_finish_pair_empty_pin(self) -> None:
        # Some device families don't show a PIN — empty string is valid.
        body = finish_pair(device_id="x", challenge_type=1, token=1, pin="")
        assert body["RESPONSE_VALUE"] == ""

    def test_cancel_pair(self) -> None:
        body = cancel_pair(device_id="ha-coord", device_name="HomeAssistant")
        assert body == {"DEVICE_ID": "ha-coord", "DEVICE_NAME": "HomeAssistant"}


class TestLaunchApp:
    """Launch-app payload — wraps an AppConfig in the device's nested shape."""

    def test_launch_with_message(self) -> None:
        config = AppConfig(app_id="3", name_space=2, message="https://hulu.com")
        body = launch_app(config)
        assert body == {
            "VALUE": {
                "APP_ID": "3",
                "NAME_SPACE": 2,
                "MESSAGE": "https://hulu.com",
            }
        }

    def test_launch_no_message(self) -> None:
        config = AppConfig(app_id="1", name_space=4)
        body = launch_app(config)
        # MESSAGE: None must be present in the body — the device expects
        # the key, not an absence.
        assert body == {"VALUE": {"APP_ID": "1", "NAME_SPACE": 4, "MESSAGE": None}}


class TestSetInput:
    """set_input PUT — same MODIFY shape as write_setting but with input
    name as VALUE."""

    def test_basic(self) -> None:
        body = set_input(name="HDMI-2", hashval=42)
        assert body == {"VALUE": "HDMI-2", "HASHVAL": 42, "REQUEST": "MODIFY"}

    def test_input_with_dash(self) -> None:
        # Input names contain dashes; passed through verbatim.
        body = set_input(name="HDMI-1", hashval=1)
        assert body["VALUE"] == "HDMI-1"

    def test_input_with_space(self) -> None:
        # User-renamed inputs may have spaces.
        body = set_input(name="Living Room PS5", hashval=1)
        assert body["VALUE"] == "Living Room PS5"


class TestPayloadIsolation:
    """Builders return fresh dicts — caller mutations don't leak between
    requests."""

    def test_write_setting_returns_fresh_dict(self) -> None:
        a = write_setting(value=1, hashval=1)
        b = write_setting(value=2, hashval=2)
        a["VALUE"] = "mutated"
        assert b["VALUE"] == 2

    def test_key_press_returns_fresh_dict(self) -> None:
        a = key_press([(11, 1)])
        b = key_press([(11, 0)])
        a["KEYLIST"].append({"X": "Y"})
        assert len(b["KEYLIST"]) == 1
