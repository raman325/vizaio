"""
Remote key codes for each device family.

Keys are ``(codeset, code)`` tuples sent on the wire as
``{"CODESET": codeset, "CODE": code, "ACTION": "KEYPRESS"}``.

Use :class:`RemoteKey` for type-safe key references; ``send_key`` also
accepts the bare string for power users.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

KeyCode = tuple[int, int]


class RemoteKey(StrEnum):
    """Canonical key names. Membership varies by device profile."""

    # Power
    POW_ON = "POW_ON"
    POW_OFF = "POW_OFF"
    POW_TOGGLE = "POW_TOGGLE"

    # Volume / mute
    VOL_UP = "VOL_UP"
    VOL_DOWN = "VOL_DOWN"
    MUTE_ON = "MUTE_ON"
    MUTE_OFF = "MUTE_OFF"
    MUTE_TOGGLE = "MUTE_TOGGLE"

    # Channel (TV)
    CH_UP = "CH_UP"
    CH_DOWN = "CH_DOWN"
    CH_PREV = "CH_PREV"

    # Media
    PLAY = "PLAY"
    PAUSE = "PAUSE"
    SEEK_FWD = "SEEK_FWD"
    SEEK_BACK = "SEEK_BACK"

    # Input
    INPUT_NEXT = "INPUT_NEXT"

    # Navigation (TV)
    EXIT = "EXIT"
    BACK = "BACK"
    OK = "OK"
    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    MENU = "MENU"
    INFO = "INFO"
    GUIDE = "GUIDE"
    SMARTCAST = "SMARTCAST"

    # Numeric (TV)
    NUM_0 = "NUM_0"
    NUM_1 = "NUM_1"
    NUM_2 = "NUM_2"
    NUM_3 = "NUM_3"
    NUM_4 = "NUM_4"
    NUM_5 = "NUM_5"
    NUM_6 = "NUM_6"
    NUM_7 = "NUM_7"
    NUM_8 = "NUM_8"
    NUM_9 = "NUM_9"


# Per-device-family key tables. Stored as plain dicts so they can live on
# DeviceProfile without forcing custom-profile users to subclass anything.

TV_KEYS: Final[dict[str, KeyCode]] = {
    RemoteKey.POW_ON: (11, 1),
    RemoteKey.POW_OFF: (11, 0),
    RemoteKey.POW_TOGGLE: (11, 2),
    RemoteKey.VOL_UP: (5, 1),
    RemoteKey.VOL_DOWN: (5, 0),
    # Audio codeset per the official app's enum ordering (7 audio keys:
    # vol x2, mute x3, MTS x2) and every community table (exiva, pyvizio,
    # openHAB): 2=off, 3=on, 4=toggle. (5,5) is Audio MTS, not mute —
    # protocol-notes V6 observed it returning FAILURE on TV firmware.
    RemoteKey.MUTE_OFF: (5, 2),
    RemoteKey.MUTE_ON: (5, 3),
    RemoteKey.MUTE_TOGGLE: (5, 4),
    RemoteKey.CH_UP: (8, 1),
    RemoteKey.CH_DOWN: (8, 0),
    RemoteKey.CH_PREV: (8, 2),
    RemoteKey.PLAY: (2, 3),
    RemoteKey.PAUSE: (2, 2),
    # Transport seek — official app enum: TRANSPORT_FAST_REVERSE = (2, 1)
    RemoteKey.SEEK_FWD: (2, 0),
    RemoteKey.SEEK_BACK: (2, 1),
    RemoteKey.INPUT_NEXT: (7, 1),
    RemoteKey.EXIT: (4, 3),
    RemoteKey.BACK: (4, 0),
    RemoteKey.OK: (3, 2),
    RemoteKey.UP: (3, 8),
    RemoteKey.DOWN: (3, 0),
    RemoteKey.LEFT: (3, 1),
    RemoteKey.RIGHT: (3, 7),
    RemoteKey.MENU: (4, 8),
    RemoteKey.INFO: (4, 6),
    RemoteKey.GUIDE: (4, 4),
    RemoteKey.SMARTCAST: (4, 3),
    # Numeric keys — community-documented codeset 0 codes 0..9. Used for
    # direct channel entry on tuner-equipped models (M-series, P-series).
    # Tuner-less models (e.g., VHD24M-0810) reject these with FAILURE,
    # which is the right user-visible behavior — the codes exist, the
    # hardware just doesn't have a channel to enter.
    RemoteKey.NUM_0: (0, 0),
    RemoteKey.NUM_1: (0, 1),
    RemoteKey.NUM_2: (0, 2),
    RemoteKey.NUM_3: (0, 3),
    RemoteKey.NUM_4: (0, 4),
    RemoteKey.NUM_5: (0, 5),
    RemoteKey.NUM_6: (0, 6),
    RemoteKey.NUM_7: (0, 7),
    RemoteKey.NUM_8: (0, 8),
    RemoteKey.NUM_9: (0, 9),
}

SOUNDBAR_KEYS: Final[dict[str, KeyCode]] = {
    RemoteKey.POW_ON: (11, 1),
    RemoteKey.POW_OFF: (11, 0),
    RemoteKey.POW_TOGGLE: (11, 2),
    RemoteKey.VOL_UP: (5, 1),
    RemoteKey.VOL_DOWN: (5, 0),
    RemoteKey.MUTE_OFF: (5, 2),
    RemoteKey.MUTE_ON: (5, 3),
    RemoteKey.MUTE_TOGGLE: (5, 4),
    RemoteKey.PLAY: (2, 3),
    RemoteKey.PAUSE: (2, 2),
    RemoteKey.INPUT_NEXT: (7, 1),
}

CRAVE360_KEYS: Final[dict[str, KeyCode]] = dict(SOUNDBAR_KEYS)
