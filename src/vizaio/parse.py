"""
High-level parsing helpers built on top of :class:`Response`.

Most callers use ``Response.require_item(cname)`` directly. The helpers
here exist for shapes that need more than a single item access — settings
options merging, input list filtering by enabled/visible flags (per
APK), app config extraction.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import VizioNotFoundError, VizioResponseError
from .types import (
    AppConfig,
    InputInfo,
    PairChallenge,
    SettingInfo,
    SettingType,
    StateExtended,
    SystemVersions,
)
from .wire import Item, Response


def parse_inputs(
    response: Response, current_input_name: str | None = None
) -> list[InputInfo]:
    """
    Parse the inputs list response.

    Per protocol-notes #6 (APK corrected): the official app filters by
    ``enabled OR visible`` flags, not by cname. The synthetic
    ``current_input`` entry — if present — is used to mark the matching
    real input as ``is_current``; if not present, the caller should pass
    ``current_input_name`` from the separate ``devices/current_input``
    fetch.
    """
    current_meta_name: str | None = current_input_name
    real_inputs: list[Item] = []
    for item in response.items:
        if item.cname == "current_input":
            value = item.value
            if isinstance(value, str):
                current_meta_name = value
            elif isinstance(value, Mapping):
                # Some firmware nests it as {"name": ...}
                current_meta_name = str(value.get("name", current_meta_name))
            continue
        real_inputs.append(item)

    result: list[InputInfo] = []
    for item in real_inputs:
        # Filter inputs by enabled/visible per APK SCPLCommandsManager.
        if not _is_input_visible(item):
            continue
        meta_name = _input_meta_name(item)
        display_name = item.name or item.cname.upper()
        # Device's ``current_input`` value is *inconsistent* across input
        # types: for CAST it returns the meta_name ('SMARTCAST'); for
        # HDMI inputs it returns the display name ('HDMI-2') even when
        # meta_name has been renamed (e.g., 'Mac'). Match against
        # either form to populate is_current correctly across firmware
        # quirks. Verified live on VHD24M-0810 fw 3.720.9.1-1.
        is_current = current_meta_name is not None and current_meta_name in (
            meta_name,
            display_name,
            item.cname,
        )
        result.append(
            InputInfo(
                name=display_name,
                meta_name=meta_name,
                is_current=is_current,
                cname=item.cname,
            )
        )
    return result


def parse_current_input(response: Response) -> str:
    """Extract the active input's user-visible name."""
    item = response.require_item("current_input")
    value = item.value
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        name = value.get("name")
        if isinstance(name, str):
            return name
    raise VizioResponseError(
        f"current_input has unexpected VALUE shape: {type(value).__name__}"
    )


def parse_current_input_item(response: Response) -> Item:
    """Return the raw current-input item (used by set_input for hashval)."""
    return response.require_item("current_input")


def parse_setting(item: Item, *, setting_type: str) -> SettingInfo:
    """Build a :class:`SettingInfo` from one Item."""
    return SettingInfo(
        setting_type=setting_type,
        name=item.cname,
        value=item.value if item.value is not None else "",
        hashval=item.hashval if item.hashval is not None else 0,
        type=_coerce_setting_type(item.type),
        min=item.min,
        max=item.max,
        center=item.center,
        options=item.elements,
    )


def parse_settings(
    settings_response: Response,
    options_response: Response | None,
    *,
    setting_type: str,
) -> dict[str, SettingInfo]:
    """Merge the settings list with their options into a dict."""
    options_by_name: dict[str, Item] = {}
    if options_response is not None:
        for item in options_response.items:
            options_by_name[item.cname] = item

    out: dict[str, SettingInfo] = {}
    for item in settings_response.items:
        merged = item
        opts = options_by_name.get(item.cname)
        if opts is not None:
            merged = Item(
                cname=item.cname,
                type=item.type,
                name=item.name,
                value=item.value,
                hashval=item.hashval,
                enabled=item.enabled,
                elements=opts.elements or item.elements,
                min=opts.min if opts.min is not None else item.min,
                max=opts.max if opts.max is not None else item.max,
                center=opts.center if opts.center is not None else item.center,
                raw=item.raw,
            )
        out[item.cname] = parse_setting(merged, setting_type=setting_type)
    return out


def parse_setting_types(response: Response) -> list[str]:
    """
    Return all menu items as the device reports them.

    Per protocol-notes #7 (APK corrected): NO filtering by name. The
    pyvizio filter for cast/input/devices/network was a pyvizio invention.
    """
    return [item.cname for item in response.items if item.type == SettingType.MENU]


def parse_current_app_config(response: Response) -> AppConfig | None:
    """
    Extract the current app's launch config, or ``None`` if no app is running.

    Per protocol-notes #9: the device may emit ``VALUE: null`` or omit
    ``VALUE`` entirely. Both shapes resolve to ``None`` here.
    """
    if not response.items:
        return None
    item = response.items[0]
    value = item.value
    if value is None or not isinstance(value, Mapping):
        return None
    app_id = value.get("app_id")
    name_space = value.get("name_space")
    message = value.get("message")
    if app_id is None and name_space is None:
        return None
    return AppConfig(
        app_id=str(app_id) if app_id is not None else "",
        name_space=int(name_space) if name_space is not None else 0,
        message=str(message) if message is not None else None,
    )


def parse_state_extended(payload: Mapping[str, Any]) -> StateExtended:
    """
    Parse the ``/state_extended`` envelope (flat-keyed, no STATUS/ITEMS).

    The on-the-wire payload differs from every other SCPL endpoint:
    no ``STATUS`` wrapper, no ``ITEMS`` array, fields appear as flat
    top-level keys (``POWER_MODE``, ``APP_CURRENT``, …). We accept it
    as a raw ``Mapping`` rather than going through :class:`Response`
    because the wire-boundary normalizer is built around the
    standard envelope. Caller (``Vizio.get_state_extended``) feeds us
    the raw parsed JSON.

    Tolerant: missing fields degrade to empty string / ``None`` /
    empty tuple rather than raising. Hardware varies across firmware
    revisions and we'd rather degrade gracefully than fail a poll
    because a single field shape changed.
    """
    ci_payload = _ci_get(payload)

    power_status = ci_payload.get("power_status")
    power_on = False
    if isinstance(power_status, Mapping):
        power_on = bool(_ci_get(power_status).get("value", 0))

    power_mode_raw = ci_payload.get("power_mode")
    power_mode = ""
    if isinstance(power_mode_raw, Mapping):
        power_mode = str(_ci_get(power_mode_raw).get("value", ""))

    current_input_raw = ci_payload.get("current_input")
    current_input = ""
    current_input_hashval: int | None = None
    if isinstance(current_input_raw, Mapping):
        ci = _ci_get(current_input_raw)
        current_input = str(ci.get("name", ""))
        hv = ci.get("hashval")
        try:
            current_input_hashval = int(hv) if hv is not None else None
        except (TypeError, ValueError):
            current_input_hashval = None

    app_current_raw = ci_payload.get("app_current")
    current_app: AppConfig | None = None
    if isinstance(app_current_raw, Mapping):
        ac = _ci_get(app_current_raw)
        app_id = ac.get("app_id")
        name_space = ac.get("name_space")
        if app_id is not None and name_space is not None:
            try:
                current_app = AppConfig(
                    app_id=str(app_id),
                    name_space=int(name_space),
                    message=str(ac["message"])
                    if ac.get("message") is not None
                    else None,
                )
            except (TypeError, ValueError):
                current_app = None

    errors_raw = ci_payload.get("errors")
    errors: tuple[str, ...] = ()
    if isinstance(errors_raw, list):
        errors = tuple(str(e) for e in errors_raw)

    return StateExtended(
        power_on=power_on,
        power_mode=power_mode,
        current_input=current_input,
        current_input_hashval=current_input_hashval,
        current_app=current_app,
        screen_mode=str(ci_payload.get("screen_mode", "")),
        media_state=str(ci_payload.get("media_state", "")),
        device_name=str(ci_payload.get("device_name", "")),
        errors=errors,
        raw=payload,
    )


def parse_system_versions(payload: Mapping[str, Any]) -> SystemVersions:
    """
    Parse ``GET /system/versions`` → :class:`SystemVersions`.

    Wire shape: ``{"STATUS": ..., "ITEM": {"VALUE": {<version map>}}}``.
    The version map keys arrive verbatim (e.g. ``"FIRMWARE"``,
    ``"SERIAL NUMBER"``, ``"SCPL"``, ``"acr"``); we surface the common
    ones typed and keep the whole map on ``raw``. Tolerant of missing
    pieces — degrades to empty strings rather than raising.
    """
    item = _ci_get(payload).get("item")
    value: Mapping[str, Any] = {}
    if isinstance(item, Mapping):
        candidate = _ci_get(item).get("value")
        if isinstance(candidate, Mapping):
            value = candidate
    lowered = {str(k).lower(): v for k, v in value.items()}

    def _field(key: str) -> str:
        found = lowered.get(key)
        return str(found) if found is not None else ""

    return SystemVersions(
        firmware=_field("firmware"),
        serial_number=_field("serial number"),
        esn=_field("esn"),
        scpl=_field("scpl"),
        raw=value,
    )


def _ci_get(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    """
    Return a case-insensitive view of a mapping by lowercasing keys.

    The standard SCPL response uses ``Response.from_json`` which
    normalizes everywhere. ``/state_extended`` doesn't go through
    that path, so its keys arrive in their original UPPERCASE form
    (``POWER_MODE``, ``APP_CURRENT``, …). Lowercase here keeps
    callers immune to firmware-side casing changes.
    """
    return {str(k).lower(): v for k, v in mapping.items()}


def parse_pair_challenge(response: Response) -> PairChallenge:
    """Extract the challenge from a begin_pair response."""
    if not response.items:
        raise VizioNotFoundError("begin_pair response missing ITEM")
    item_value = response.items[0]
    raw = item_value.raw
    challenge_type = raw.get("challenge_type")
    token = raw.get("pairing_req_token")
    if challenge_type is None or token is None:
        raise VizioResponseError(
            "begin_pair response missing CHALLENGE_TYPE or PAIRING_REQ_TOKEN"
        )
    return PairChallenge(
        challenge_type=int(challenge_type),
        token=int(token),
    )


def parse_auth_token(response: Response) -> str:
    """Extract the auth token from a finish_pair response."""
    if not response.items:
        raise VizioNotFoundError("finish_pair response missing ITEM")
    raw = response.items[0].raw
    token = raw.get("auth_token")
    if not isinstance(token, str):
        raise VizioResponseError("finish_pair response missing AUTH_TOKEN")
    return token


def parse_device_info(response: Response) -> dict[str, str]:
    """
    Extract the device info dict (model_name, name, etc.).

    Returned dict has lowercased keys (already normalized by
    :meth:`Response.from_json`).
    """
    if not response.items:
        return {}
    value = response.items[0].value
    if not isinstance(value, Mapping):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


def parse_model_name(response: Response, *, settings_root: str) -> str:
    """
    Extract the model name from a device info response.

    TVs use ``MODEL_NAME``; soundbars use ``NAME``.
    """
    info = parse_device_info(response)
    if settings_root == "tv_settings":
        return info.get("model_name", "")
    return info.get("name", "")


def parse_vizios_binary(response: Response) -> str:
    """
    Extract ``BINARIES.ViziOS`` from a device info response.

    Returns the bare string (e.g., ``"mtk5583-1.6.1312.1"``) so callers
    can hand it to :func:`apps.extract_chipset`. Empty string when
    absent — older firmware may not expose it.
    """
    if not response.items:
        return ""
    value = response.items[0].value
    if not isinstance(value, Mapping):
        return ""
    binaries = value.get("binaries")
    if not isinstance(binaries, Mapping):
        return ""
    vizios = binaries.get("vizios")
    return str(vizios) if vizios else ""


def parse_system_info_model_name(response: Response) -> str:
    """
    Extract ``SYSTEM_INFO.MODEL_NAME`` — the canonical model identifier.

    Distinct from :func:`parse_model_name`, which returns the friendly
    ``NAME`` field for non-TV settings roots. Crave-prefix matching
    needs the model identifier (``"SP30-E0"``), not the friendly name
    (``"Crave Go"``).

    Returns the bare string. Empty string when absent — older firmware
    may not expose the nested SYSTEM_INFO block.
    """
    if not response.items:
        return ""
    value = response.items[0].value
    if not isinstance(value, Mapping):
        return ""
    system_info = value.get("system_info")
    if not isinstance(system_info, Mapping):
        return ""
    model_name = system_info.get("model_name")
    if not isinstance(model_name, str):
        return ""
    return model_name


def parse_firmware_version(response: Response) -> str:
    """
    Extract ``SYSTEM_INFO.VERSION`` from a device info response.

    Distinct from :meth:`Vizio.get_version`: that hits the legacy
    per-field path (``firmware`` cname) and is what users print. This
    helper reads the same value but from the deviceinfo aggregate, so
    the chipset+firmware availability lookup can be done in one
    request without a second round-trip. Empty string when absent.
    """
    if not response.items:
        return ""
    value = response.items[0].value
    if not isinstance(value, Mapping):
        return ""
    system_info = value.get("system_info")
    if not isinstance(system_info, Mapping):
        return ""
    version = system_info.get("version")
    return str(version) if version else ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_input_visible(item: Item) -> bool:
    """
    Return whether the input should appear in user-facing input lists.

    Per APK ``SCPLCommandsManager.sortedAvailableInputs``, include inputs
    where ``enabled`` OR ``visible`` is truthy. The flags may not be
    present on older firmware — when absent, treat
    as visible (the legacy behavior).
    """
    raw = item.raw
    enabled = _bool_or_default(raw.get("enabled"), default=True)
    visible = _bool_or_default(raw.get("visible"), default=True)
    return enabled or visible


def _bool_or_default(value: Any, *, default: bool) -> bool:
    """
    Coerce a wire-string or numeric value to bool, falling back to ``default``.

    Recognized truthy strings: ``"true"``, ``"yes"``, ``"on"``, ``"1"``
    (case-insensitive). ``None`` or any unrecognized type returns
    ``default``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "yes", "on", "1")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _input_meta_name(item: Item) -> str:
    """
    Extract the user-visible name of an input.

    Inputs return ``VALUE`` as a dict ``{"name": "PS5", "metadata": ""}``
    or sometimes as a bare string. Empty meta_name is valid (input has
    no custom name).
    """
    value = item.value
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        name = value.get("name")
        if isinstance(name, str):
            return name
    return ""


def _coerce_setting_type(raw: str) -> SettingType:
    """Map raw type string to :class:`SettingType`. Unknown → MENU."""
    try:
        return SettingType(raw)
    except ValueError:
        return SettingType.MENU
