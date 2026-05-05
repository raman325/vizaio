"""Wire-format fixture factories — ground truth for what the device sends.

Adapted from `pyvizio/tests/conftest.py`. These are pure JSON shape
factories: dict in, dict out. They have no dependency on `vizio_smartcast`
or any HTTP client, so they can be used to test parsing in isolation
(``test_wire.py``) and to drive higher-level tests via ``aioresponses``.

Casing convention: device responses come back in **mixed casing** in the
wild (see ``docs/protocol-notes.md`` quirk #1). To exercise our
normalization layer, every factory accepts a ``casing`` arg that emits
``"upper"``, ``"lower"``, or ``"mixed"`` keys. Tests should run against
multiple casings to prove ``Response.from_json`` is case-stable.
"""

from __future__ import annotations

from typing import Any, Literal

# ---------------------------------------------------------------------------
# Connection constants used by HTTP-mocking tests
# ---------------------------------------------------------------------------

TV_HOST = "192.168.1.100"
TV_PORT = 7345
TV_HOST_PORT = f"{TV_HOST}:{TV_PORT}"

SOUNDBAR_HOST = "192.168.1.101"
SOUNDBAR_PORT = 9000
SOUNDBAR_HOST_PORT = f"{SOUNDBAR_HOST}:{SOUNDBAR_PORT}"

CRAVE_HOST = "192.168.1.102"
CRAVE_PORT = 9000
CRAVE_HOST_PORT = f"{CRAVE_HOST}:{CRAVE_PORT}"

AUTH_TOKEN = "auth-token-fixture-123"

Casing = Literal["upper", "lower", "mixed"]


# ---------------------------------------------------------------------------
# Casing helpers
# ---------------------------------------------------------------------------


def _key(name: str, casing: Casing = "upper") -> str:
    """Return ``name`` cased per the convention.

    Device responses use uppercase keys most of the time but real captures
    show inconsistencies. ``mixed`` simulates one realistic mix.
    """
    if casing == "upper":
        return name.upper()
    if casing == "lower":
        return name.lower()
    # mixed: alternate per char to exercise our case-insensitive parser
    return "".join(c.lower() if i % 2 else c.upper() for i, c in enumerate(name))


def _envelope(
    *,
    items: list[dict[str, Any]] | None = None,
    item: dict[str, Any] | None = None,
    result: str = "SUCCESS",
    detail: str = "Success",
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Build a minimal valid response envelope."""
    k = lambda s: _key(s, casing)  # noqa: E731 — local convenience
    resp: dict[str, Any] = {
        k("STATUS"): {k("RESULT"): result, k("DETAIL"): detail},
    }
    if items is not None:
        resp[k("ITEMS")] = items
    if item is not None:
        resp[k("ITEM")] = item
    return resp


# ---------------------------------------------------------------------------
# Generic item / response factories
# ---------------------------------------------------------------------------


def make_item(
    cname: str,
    value: Any,
    *,
    hashval: int | None = 1,
    item_type: str = "T_VALUE_V1",
    name: str | None = None,
    casing: Casing = "upper",
    **extra: Any,
) -> dict[str, Any]:
    """A single ITEM dict.

    ``extra`` is folded in verbatim (uppercase keys) — used for ELEMENTS,
    MINIMUM, MAXIMUM, CENTER, ENABLED, etc.
    """
    k = lambda s: _key(s, casing)  # noqa: E731
    item: dict[str, Any] = {
        k("CNAME"): cname,
        k("TYPE"): item_type,
        k("NAME"): name or cname,
        k("VALUE"): value,
    }
    if hashval is not None:
        item[k("HASHVAL")] = hashval
    for ek, ev in extra.items():
        item[k(ek)] = ev
    return item


def make_success_response(
    *,
    items: list[dict[str, Any]] | None = None,
    item: dict[str, Any] | None = None,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Generic success envelope."""
    return _envelope(items=items, item=item, casing=casing)


def make_error_response(
    *,
    result: str = "INVALID_PARAMETER",
    detail: str = "invalid value specified",
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Error envelope. Result values: SUCCESS, INVALID_PARAMETER, FAILURE,
    REQUIRES_PAIRING, PAIRING_DENIED, BLOCKED."""
    return _envelope(result=result, detail=detail, casing=casing)


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------


def make_power_response(value: int, *, casing: Casing = "upper") -> dict[str, Any]:
    """Power state. ``value`` is 1 (on) or 0 (off)."""
    return make_success_response(
        items=[make_item("power_mode", value, name="Power Mode", casing=casing)],
        casing=casing,
    )


def make_key_press_response(*, casing: Casing = "upper") -> dict[str, Any]:
    """Empty success response from a key press PUT."""
    return make_success_response(casing=casing)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def make_input_item(
    cname: str,
    display_name: str,
    meta_name: str,
    hashval: int,
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Single input. ``meta_name`` is the user-customized name on the TV."""
    k = lambda s: _key(s, casing)  # noqa: E731
    return make_item(
        cname,
        {k("NAME"): meta_name, k("METADATA"): ""},
        hashval=hashval,
        name=display_name,
        casing=casing,
    )


def make_inputs_list_response(
    inputs: list[tuple[str, str, str, int]],
    *,
    include_synthetic_current: bool = True,
    current_input_meta_name: str = "HDMI-1",
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Inputs list response.

    ``inputs`` is a list of ``(cname, display_name, meta_name, hashval)``.

    When ``include_synthetic_current`` is True (the pyvizio-observed
    behavior — see protocol-notes.md quirk #6), we append a synthetic
    item with cname="current_input" indicating which input is selected.
    Tests should run BOTH ways — our parser must work whether or not the
    synthetic item is present (hardware verification pending).
    """
    items = [
        make_input_item(cname, display, meta, hv, casing=casing)
        for cname, display, meta, hv in inputs
    ]
    if include_synthetic_current:
        items.append(
            make_item(
                "current_input",
                current_input_meta_name,
                hashval=0,
                name="Current Input",
                casing=casing,
            )
        )
    return make_success_response(items=items, casing=casing)


def make_current_input_response(
    cname: str,
    meta_name: str,
    hashval: int,
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Standalone /current_input endpoint response — the simpler shape used
    by ``set_input`` to fetch the active hashval."""
    return make_success_response(
        items=[
            make_item(
                cname,
                meta_name,
                hashval=hashval,
                name="Current Input",
                casing=casing,
            )
        ],
        casing=casing,
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def make_setting_types_response(
    types: list[str],
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Setting categories (audio, picture, system, etc.)."""
    items = [
        make_item(cname, "", item_type="T_MENU_V1", name=cname.title(), casing=casing)
        for cname in types
    ]
    return make_success_response(items=items, casing=casing)


def make_settings_response(
    settings: list[tuple[str, Any, str, int]],
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """All settings under a category.

    ``settings`` is a list of ``(cname, value, item_type, hashval)``.
    """
    items = [
        make_item(cname, value, hashval=hashval, item_type=item_type, casing=casing)
        for cname, value, item_type, hashval in settings
    ]
    return make_success_response(items=items, casing=casing)


def make_settings_options_response(
    settings: list[dict[str, Any]],
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Settings options (min/max/center for sliders, ELEMENTS for lists).

    Each settings dict needs ``cname`` and ``item_type``; may include
    ``MINIMUM``, ``MAXIMUM``, ``CENTER``, ``ELEMENTS`` (uppercase keys).
    """
    items = []
    for s in settings:
        extras = {
            k: s[k] for k in ("MINIMUM", "MAXIMUM", "CENTER", "ELEMENTS") if k in s
        }
        items.append(
            make_item(
                s["cname"],
                s.get("value", ""),
                hashval=s.get("hashval", 1),
                item_type=s["item_type"],
                casing=casing,
                **extras,
            )
        )
    return make_success_response(items=items, casing=casing)


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def make_pair_begin_response(
    challenge_type: int,
    token: int,
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """begin_pair response — uses ITEM (singular), not ITEMS."""
    k = lambda s: _key(s, casing)  # noqa: E731
    return make_success_response(
        item={k("CHALLENGE_TYPE"): challenge_type, k("PAIRING_REQ_TOKEN"): token},
        casing=casing,
    )


def make_pair_finish_response(
    auth_token: str,
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """finish_pair response — auth token in ITEM."""
    k = lambda s: _key(s, casing)  # noqa: E731
    return make_success_response(
        item={k("AUTH_TOKEN"): auth_token},
        casing=casing,
    )


def make_pair_cancel_response(*, casing: Casing = "upper") -> dict[str, Any]:
    """cancel_pair response — empty success."""
    return make_success_response(casing=casing)


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------


def make_current_app_response(
    app_id: str,
    name_space: int,
    message: str | None = None,
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """current_app response — config tuple in ITEM.VALUE."""
    k = lambda s: _key(s, casing)  # noqa: E731
    return make_success_response(
        item={
            k("VALUE"): {
                k("APP_ID"): app_id,
                k("NAME_SPACE"): name_space,
                k("MESSAGE"): message,
            }
        },
        casing=casing,
    )


def make_no_app_response(
    *,
    value_present: bool = True,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """current_app response when no app is running.

    The device may either return ``VALUE: null`` (``value_present=True``)
    or omit ``VALUE`` entirely (``value_present=False``). Both cases must
    parse to ``None`` — see protocol-notes.md quirk #9.
    """
    k = lambda s: _key(s, casing)  # noqa: E731
    item: dict[str, Any] = {}
    if value_present:
        item[k("VALUE")] = None
    return make_success_response(item=item, casing=casing)


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------


def make_device_info_response(
    info: dict[str, Any],
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Device info — model name, system info, etc., in ITEMS[0].VALUE."""
    k = lambda s: _key(s, casing)  # noqa: E731
    keyed = {k(field): v for field, v in info.items()}
    return make_success_response(items=[{k("VALUE"): keyed}], casing=casing)


# ---------------------------------------------------------------------------
# Battery / charging (Crave 360 only)
# ---------------------------------------------------------------------------


def make_battery_level_response(
    level: int,
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Battery level 0-100."""
    return make_success_response(
        items=[make_item("battery_level", level, name="Battery Level", casing=casing)],
        casing=casing,
    )


def make_charging_status_response(
    status: int,
    *,
    casing: Casing = "upper",
) -> dict[str, Any]:
    """Charging status 0=not charging, 1=charging, 2=fully charged."""
    return make_success_response(
        items=[
            make_item("charging_status", status, name="Charging Status", casing=casing)
        ],
        casing=casing,
    )


# ---------------------------------------------------------------------------
# Real-shape conveniences for typical scenarios
# ---------------------------------------------------------------------------

DEFAULT_TV_INPUTS = [
    ("hdmi1", "HDMI-1", "Living Room TV", 1),
    ("hdmi2", "HDMI-2", "PS5", 2),
    ("hdmi3", "HDMI-3", "", 3),
    ("smartcast", "SmartCast", "", 4),
    ("comp", "COMP", "", 5),
]
"""Five-input TV layout used by most behavior tests."""


DEFAULT_AUDIO_SETTINGS = [
    ("volume", 25, "T_VALUE_ABS_V1", 100),
    ("mute", "Off", "T_LIST_V1", 101),
    ("eq", "Normal", "T_LIST_V1", 102),
    ("surround", "Music", "T_LIST_X_V1", 103),
]
"""Standard TV audio settings layout."""


DEFAULT_AUDIO_OPTIONS = [
    {"cname": "volume", "item_type": "T_VALUE_ABS_V1", "MINIMUM": 0, "MAXIMUM": 100},
    {"cname": "mute", "item_type": "T_LIST_V1", "ELEMENTS": ["Off", "On"]},
    {
        "cname": "eq",
        "item_type": "T_LIST_V1",
        "ELEMENTS": ["Normal", "Music", "Movie", "Game"],
    },
    {
        "cname": "bass",
        "item_type": "T_VALUE_ABS_V1",
        "MINIMUM": -6,
        "MAXIMUM": 6,
        "CENTER": 0,
    },
]
"""Audio settings options matching DEFAULT_AUDIO_SETTINGS shape."""
