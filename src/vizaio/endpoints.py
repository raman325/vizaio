"""
Endpoint catalog — declarative table.

The whole SmartCast protocol surface lives in :data:`ENDPOINTS` below.
Each row maps an :class:`Endpoint` enum member to its specification:
HTTP method, path template(s), auth model, the item cname expected in
successful responses, and the device-profile capabilities the endpoint
requires.

Reading order:

1. :class:`Endpoint` — symbolic names of every endpoint.
2. :data:`ENDPOINTS` — the table. The whole protocol on one screen.
3. :func:`resolve` — turns a (Endpoint, DeviceProfile) pair into a
   :class:`ResolvedSpec` ready for :class:`SmartCastClient`.

Path templates use ``{root}`` and ``{root_static}`` for the device's
settings-tree root (``tv_settings`` or ``audio_settings``). Anything
else is a literal path.

Auth model:

- ``NONE`` — endpoint always works without auth (state probes,
  pairing).
- ``REQUIRED`` — always needs auth (apps, bulk state).
- ``PROFILE`` — defer to ``DeviceProfile.requires_auth``. TVs need
  auth; soundbars/Crave don't.

Capability gating: each endpoint declares the :class:`Need` flags it
requires. ``resolve()`` checks them against the profile before any
HTTP and raises :class:`VizioUnsupportedError` for missing capabilities
— so e.g. ``Endpoint.LAUNCH_APP`` on a soundbar fails immediately, not
with a 404.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Flag, StrEnum, auto
from typing import Literal

from .errors import VizioUnsupportedError
from .types import AuthRequirement, DeviceProfile

# ---------------------------------------------------------------------------
# Settings-tree roots
# ---------------------------------------------------------------------------


class SettingsRoot(StrEnum):
    """Root namespace under ``/menu_native/`` for a device's settings tree."""

    TV = "tv_settings"
    AUDIO = "audio_settings"


# ---------------------------------------------------------------------------
# Endpoint enum — every protocol surface vizaio knows about
# ---------------------------------------------------------------------------


class Endpoint(StrEnum):
    """Symbolic endpoint names. See :data:`ENDPOINTS` for the spec."""

    # State (always unauthed where possible)
    DEVICE_INFO = "device_info"
    POWER_MODE = "power_mode"
    STATE_EXTENDED = "state_extended"
    SYSTEM_VERSIONS = "system_versions"
    BATTERY_LEVEL = "battery_level"
    CHARGING_STATUS = "charging_status"

    # Control
    KEY_PRESS = "key_press"
    INPUTS = "inputs"
    CURRENT_INPUT = "current_input"
    VOLUME_LEVEL = "volume_level"

    # Flat volume family ("volume API V2"). Available only on firmware
    # whose API spec clears ``ApiSpec.V2_0_0_2031_0014`` — see
    # :func:`vizaio.apispec.supports_volume_v2`. Resolving these does not
    # check that threshold (the profile has no way to know it without a
    # deviceinfo round trip); ``Vizio`` gates the call sites instead.
    VOLUME_LEVEL_STATUS = "volume_level_status"
    VOLUME_MUTE_STATUS = "volume_mute_status"
    VOLUME_MUTE_SET = "volume_mute_set"
    VOLUME_INCREASE = "volume_increase"
    VOLUME_DECREASE = "volume_decrease"

    # Apps (TV only)
    CURRENT_APP = "current_app"
    LAUNCH_APP = "launch_app"

    # Settings tree
    SETTINGS = "settings"
    SETTINGS_OPTIONS = "settings_options"

    # Identity (multi-path firmware fallback)
    ESN = "esn"
    SERIAL_NUMBER = "serial_number"
    VERSION = "version"
    TV_INFORMATION = "tv_information"
    """Aggregate tv_information endpoint. Modern firmware (~3.7+) returns
    all identity fields (serial_number, firmware, model_name, ...) in
    a single response and rejects per-field child paths with
    URI_NOT_FOUND. Used as a one-round-trip identity fetcher; the
    per-field endpoints stay as fallbacks for older firmware."""

    # Parental / purchase PIN
    PIN_IS_DEFAULT = "pin_is_default"

    # Pairing (always unauthed)
    BEGIN_PAIR = "begin_pair"
    FINISH_PAIR = "finish_pair"
    CANCEL_PAIR = "cancel_pair"


# ---------------------------------------------------------------------------
# Capability requirements — checked by resolve() against DeviceProfile
# ---------------------------------------------------------------------------


class Need(Flag):
    """Capabilities an endpoint requires of the device profile."""

    NONE = 0
    INPUTS = auto()
    APPS = auto()
    BATTERY = auto()


# ---------------------------------------------------------------------------
# Auth model — used by table rows; converted to AuthRequirement at resolve()
# ---------------------------------------------------------------------------


class AuthMode(StrEnum):
    """
    How an endpoint authenticates.

    Distinct from :class:`AuthRequirement` (the post-resolution shape).
    The mode is *what's declared in the table*; the requirement is *what
    that means for this specific profile*.
    """

    NONE = "none"
    """Endpoint always works without auth."""

    REQUIRED = "required"
    """Endpoint always needs an auth token."""

    PROFILE = "profile"
    """Defer to ``DeviceProfile.requires_auth`` — TVs require, audio
    devices don't."""


# ---------------------------------------------------------------------------
# Table row — paths/auth left as templates until resolve() runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Row:
    """
    One row in :data:`ENDPOINTS`.

    Internal — callers see :class:`EndpointSpec` only after
    :func:`resolve` has rendered paths against a profile.
    """

    method: Literal["GET", "PUT"]
    paths: tuple[str, ...]
    auth: AuthMode = AuthMode.PROFILE
    item: str | None = None
    needs: Need = Need.NONE


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    """
    A resolved endpoint, ready for :class:`SmartCastClient.request_spec`.

    Produced by :func:`resolve`. Callers don't construct these directly
    — except where they need to send ad-hoc one-off requests not in the
    table.
    """

    paths: tuple[str, ...]
    """Path candidates in firmware-fallback order. Most endpoints have
    one path; ESN/serial/version have two for newer/older firmware."""

    method: Literal["GET", "PUT"]
    auth: AuthRequirement
    item_cname: str | None = None
    """If set, the natural ``Response.require_item(name)`` accessor."""


def row(
    method: Literal["GET", "PUT"],
    *paths: str,
    auth: AuthMode = AuthMode.PROFILE,
    item: str | None = None,
    needs: Need = Need.NONE,
) -> _Row:
    """
    Build a table row.

    Path is positional and may repeat (firmware fallback). Used only
    inside the :data:`ENDPOINTS` literal.
    """
    return _Row(method=method, paths=tuple(paths), auth=auth, item=item, needs=needs)


# ---------------------------------------------------------------------------
# THE TABLE — read top to bottom for the whole protocol surface
# ---------------------------------------------------------------------------

# Aliases for the table — keep the rows narrow and visually aligned.
NONE = AuthMode.NONE
REQ = AuthMode.REQUIRED
PROF = AuthMode.PROFILE
_TV_INFO = "{root}/admin_and_privacy/system_information/tv_information"
_TV_INFO_LEGACY = "{root}/system/system_information/tv_information"
# ESN lives under a *different* subtree than serial/version — it's at
# ``uli_information/esn``, not ``tv_information/esn``. Verified live on
# VHD24M-0810 fw 3.720.9.1-1: pyvizio's path returns the ESN; the
# tv_information path returns URI_NOT_FOUND. Both modern and legacy
# parents are listed since some firmware exposes only one.
_ULI_INFO = "{root}/admin_and_privacy/system_information/uli_information"
_ULI_INFO_LEGACY = "{root}/system/system_information/uli_information"

ENDPOINTS: dict[Endpoint, _Row] = {
    # State —————————————————————————————————————————————————————————————
    Endpoint.DEVICE_INFO: row("GET", "/state/device/deviceinfo", auth=NONE),
    Endpoint.POWER_MODE: row("GET", "/state/device/power_mode", item="power_mode"),
    Endpoint.STATE_EXTENDED: row("GET", "/state_extended", auth=REQ),
    Endpoint.SYSTEM_VERSIONS: row("GET", "/system/versions", auth=REQ),
    Endpoint.BATTERY_LEVEL: row(
        "GET", "/state/device/battery_level", item="battery_level", needs=Need.BATTERY
    ),
    Endpoint.CHARGING_STATUS: row(
        "GET",
        "/state/device/charging_status",
        item="charging_status",
        needs=Need.BATTERY,
    ),
    # Control —————————————————————————————————————————————————————————
    Endpoint.KEY_PRESS: row("PUT", "/key_command/"),
    # Flat absolute-volume set — no HASHVAL required (verified live on
    # VHD24M-0810), unlike the menu_native audio/volume path.
    Endpoint.VOLUME_LEVEL: row("PUT", "/audio/volume/level", auth=REQ),
    # The rest of the flat volume family, present on volume-V2 firmware.
    # Reads here need no HASHVAL and are unaffected by whether ``volume``
    # appears in the ``audio`` settings *collection* — see
    # home-assistant/core#179254.
    Endpoint.VOLUME_LEVEL_STATUS: row("GET", "/audio/volume/level", auth=REQ),
    Endpoint.VOLUME_MUTE_STATUS: row("GET", "/audio/volume/mute", auth=REQ),
    Endpoint.VOLUME_MUTE_SET: row("PUT", "/audio/volume/mute", auth=REQ),
    # Relative steps. The step count goes in the **body** as
    # ``{"STEP": n}`` — NOT as the ``?STEP=n`` query string the official
    # app's generated client uses. Verified on VHD24M-0810: the query
    # form returns SUCCESS and moves the volume by exactly 1 whatever
    # the value, so sending it is a silent off-by-N. See
    # ``_payloads.volume_step`` and protocol-notes #31.
    Endpoint.VOLUME_INCREASE: row("PUT", "/audio/volume/increase", auth=REQ),
    Endpoint.VOLUME_DECREASE: row("PUT", "/audio/volume/decrease", auth=REQ),
    Endpoint.LAUNCH_APP: row("PUT", "/app/launch", auth=REQ, needs=Need.APPS),
    Endpoint.CURRENT_APP: row("GET", "/app/current", auth=REQ, needs=Need.APPS),
    # Inputs ——————————————————————————————————————————————————————————
    Endpoint.INPUTS: row(
        "GET", "{root}/devices/name_input", auth=REQ, needs=Need.INPUTS
    ),
    Endpoint.CURRENT_INPUT: row(
        "GET",
        "{root}/devices/current_input",
        auth=REQ,
        item="current_input",
        needs=Need.INPUTS,
    ),
    # Settings tree ———————————————————————————————————————————————————
    Endpoint.SETTINGS: row("GET", "{root}"),
    Endpoint.SETTINGS_OPTIONS: row("GET", "{root_static}"),
    # Identity (multi-path firmware fallback) ——————————————————————————
    Endpoint.ESN: row(
        "GET",
        f"{_ULI_INFO}/esn",
        f"{_ULI_INFO_LEGACY}/esn",
        # Defensive fallbacks — earlier versions of this library
        # mistakenly looked for ESN under tv_information/, which
        # returns URI_NOT_FOUND on every modern firmware revision.
        # Keep the wrong paths as last-resort fallbacks in case some
        # firmware revision genuinely puts ESN there.
        f"{_TV_INFO}/esn",
        f"{_TV_INFO_LEGACY}/esn",
        item="esn",
    ),
    Endpoint.SERIAL_NUMBER: row(
        "GET",
        f"{_TV_INFO}/serial_number",
        f"{_TV_INFO_LEGACY}/serial_number",
        item="serial_number",
    ),
    Endpoint.VERSION: row(
        "GET", f"{_TV_INFO}/version", f"{_TV_INFO_LEGACY}/version", item="version"
    ),
    Endpoint.TV_INFORMATION: row("GET", _TV_INFO, _TV_INFO_LEGACY),
    Endpoint.PIN_IS_DEFAULT: row("GET", "/pin/is_pin_default", auth=REQ),
    # Pairing —————————————————————————————————————————————————————————
    Endpoint.BEGIN_PAIR: row("PUT", "/pairing/start", auth=NONE),
    Endpoint.FINISH_PAIR: row("PUT", "/pairing/pair", auth=NONE),
    Endpoint.CANCEL_PAIR: row("PUT", "/pairing/cancel", auth=NONE),
}


# ---------------------------------------------------------------------------
# Path builders — exported for tests and any external callers that need
# to construct paths matching what resolve() produces.
# ---------------------------------------------------------------------------


def settings_path(root: SettingsRoot, *parts: str) -> str:
    """
    Build a path under the dynamic settings tree.

    >>> settings_path(SettingsRoot.TV, "devices", "current_input")
    '/menu_native/dynamic/tv_settings/devices/current_input'
    """
    base = f"/menu_native/dynamic/{root.value}"
    return f"{base}/{'/'.join(parts)}" if parts else base


def settings_options_path(root: SettingsRoot, *parts: str) -> str:
    """Build a path under the static settings-options tree."""
    base = f"/menu_native/static/{root.value}"
    return f"{base}/{'/'.join(parts)}" if parts else base


def state_path(*parts: str) -> str:
    """Build a top-level state path."""
    return f"/state/{'/'.join(parts)}"


def pairing_path(action: Literal["start", "pair", "cancel"]) -> str:
    """Build a pairing endpoint path."""
    return f"/pairing/{action}"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve(endpoint: Endpoint, profile: DeviceProfile) -> EndpointSpec:
    """
    Resolve an :class:`Endpoint` against a :class:`DeviceProfile`.

    Renders path templates against the profile's settings root, gates
    on the profile's capabilities, and converts the table's
    :class:`AuthMode` to a concrete :class:`AuthRequirement`.

    Raises :class:`VizioUnsupportedError` for capabilities the profile
    lacks (e.g., ``LAUNCH_APP`` on a soundbar).
    """
    raw = ENDPOINTS.get(endpoint)
    if raw is None:
        raise VizioUnsupportedError(f"Endpoint {endpoint!r} has no spec")

    _check_capabilities(endpoint, raw.needs, profile)

    paths = tuple(_render_path(p, profile) for p in raw.paths)
    auth = _resolve_auth(raw.auth, profile)
    return EndpointSpec(paths=paths, method=raw.method, auth=auth, item_cname=raw.item)


def _check_capabilities(
    endpoint: Endpoint, needs: Need, profile: DeviceProfile
) -> None:
    """Raise :class:`VizioUnsupportedError` if ``profile`` lacks any capability ``endpoint`` requires."""
    if needs is Need.NONE:
        return
    have: Need = Need.NONE
    if profile.has_inputs:
        have |= Need.INPUTS
    if profile.has_apps:
        have |= Need.APPS
    if profile.has_battery:
        have |= Need.BATTERY
    missing = needs & ~have
    if missing:
        raise VizioUnsupportedError(
            f"{profile.name} does not support {endpoint.value} (needs {missing!r})"
        )


def _render_path(template: str, profile: DeviceProfile) -> str:
    """Substitute ``{root}`` / ``{root_static}`` in a template."""
    if "{" not in template:
        return template
    return template.format(
        root=f"/menu_native/dynamic/{profile.settings_root.value}",
        root_static=f"/menu_native/static/{profile.settings_root.value}",
    )


def _resolve_auth(mode: AuthMode, profile: DeviceProfile) -> AuthRequirement:
    """Translate an :class:`AuthMode` into the concrete :class:`AuthRequirement` for ``profile``."""
    if mode is AuthMode.NONE:
        return AuthRequirement.NONE
    if mode is AuthMode.REQUIRED:
        return AuthRequirement.REQUIRED
    return AuthRequirement.REQUIRED if profile.requires_auth else AuthRequirement.NONE


__all__ = [
    "ENDPOINTS",
    "AuthMode",
    "Endpoint",
    "EndpointSpec",
    "Need",
    "SettingsRoot",
    "pairing_path",
    "resolve",
    "row",
    "settings_options_path",
    "settings_path",
    "state_path",
]
