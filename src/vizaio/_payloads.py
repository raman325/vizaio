"""
Request payload builders.

Centralizes every PUT body construction so payload shape changes have
one locus and call sites stay free of stringly-typed dicts. All builders
return plain ``dict[str, Any]`` ready to JSON-encode.

The device requires uppercase request keys (``HASHVAL``, ``VALUE``,
``REQUEST``, etc.) — we never send lowercase keys outbound, regardless
of how :mod:`wire` normalizes incoming responses.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .types import AppConfig, PairChallenge

KeyCode = tuple[int, int]
"""``(codeset, code)`` pair used in KEYLIST entries."""


def write_setting(value: Any, hashval: int) -> dict[str, Any]:
    """MODIFY a settings value with the server-issued hashval."""
    return {"VALUE": value, "HASHVAL": hashval, "REQUEST": "MODIFY"}


def volume_level(level: int) -> dict[str, Any]:
    """Body for the flat ``PUT /audio/volume/level`` — no HASHVAL needed."""
    return {"LEVEL": level}


def key_press(codes: Sequence[KeyCode]) -> dict[str, Any]:
    """
    Build a KEYLIST PUT body. ``ACTION`` is always ``KEYPRESS``.

    Per protocol-notes #19 (APK confirmed), the official app sends
    arbitrary-length lists in one PUT. No client-side chunking here —
    that's a higher-level concern.
    """
    if not codes:
        raise ValueError("key_press requires at least one code")
    return {
        "KEYLIST": [{"CODESET": cs, "CODE": c, "ACTION": "KEYPRESS"} for cs, c in codes]
    }


def begin_pair(*, device_id: str, device_name: str) -> dict[str, Any]:
    """Initiate a pairing handshake."""
    return {"DEVICE_ID": device_id, "DEVICE_NAME": device_name}


def finish_pair(
    *,
    device_id: str,
    challenge: PairChallenge,
    pin: str,
) -> dict[str, Any]:
    """Complete a pairing handshake with the user-provided PIN."""
    return {
        "DEVICE_ID": device_id,
        "CHALLENGE_TYPE": challenge.challenge_type,
        "PAIRING_REQ_TOKEN": challenge.token,
        "RESPONSE_VALUE": pin,
    }


def cancel_pair(*, device_id: str, device_name: str) -> dict[str, Any]:
    """Abort a pairing handshake."""
    return {"DEVICE_ID": device_id, "DEVICE_NAME": device_name}


def launch_app(config: AppConfig) -> dict[str, Any]:
    """Launch a SmartCast app by config tuple."""
    return {
        "VALUE": {
            "APP_ID": config.app_id,
            "NAME_SPACE": config.name_space,
            "MESSAGE": config.message,
        }
    }


def set_input(*, name: str, hashval: int) -> dict[str, Any]:
    """MODIFY the active input by name."""
    return {"VALUE": name, "HASHVAL": hashval, "REQUEST": "MODIFY"}
