"""
Public types: enums, capability profiles, and frozen dataclasses.

Design notes:
- ``DeviceProfile`` is the unit of capability variation, NOT ``DeviceType``.
  ``DeviceType`` exists as an ergonomic preset selector
  (``DeviceType.TV.profile``) but advanced users can construct custom
  profiles for unusual or future devices without waiting for a library update.
- Enums use ``StrEnum`` so they round-trip cleanly through TOML/JSON.
- Dataclasses use ``frozen=True, slots=True`` for immutability and lower
  per-instance memory cost (HA polls hard).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .endpoints import SettingsRoot


class DeviceType(StrEnum):
    """
    Preset device family. Maps to a built-in :class:`DeviceProfile`.

    Crave variants split per APK findings: Vizio's own app distinguishes
    Go (SP30-E0), 360 (SP50-D5), and Pro (SP70-D5) by model string.
    """

    TV = "tv"
    SOUNDBAR = "soundbar"
    CRAVE_GO = "crave_go"
    CRAVE360 = "crave360"
    CRAVE_PRO = "crave_pro"

    @property
    def profile(self) -> DeviceProfile:
        """Return the :class:`DeviceProfile` preset for this device family."""
        # Lazy import: profiles.py imports DeviceProfile from this module.
        from .profiles import PROFILES  # noqa: PLC0415

        return PROFILES[self]


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """
    Capability descriptor for a Vizio device class.

    Use the built-in presets (``DeviceType.TV.profile`` etc.) for the common
    cases. Construct custom profiles for unusual or future devices.
    """

    name: str
    """Human-readable name, used in error messages and CLI output."""

    settings_root: SettingsRoot
    """Which menu tree this device exposes settings under."""

    max_volume: int
    """Maximum volume value reported by the device."""

    requires_auth: bool
    """If ``True``, methods touching auth-protected endpoints raise
    :class:`VizioAuthError` when no token is configured."""

    has_battery: bool
    """If ``True``, ``get_battery_level`` and ``get_charging_status`` are
    supported. Otherwise they raise :class:`VizioUnsupportedError`."""

    has_inputs: bool
    """If ``True``, the input-list endpoints exist. Soundbars typically
    don't expose individual inputs through this API."""

    has_apps: bool
    """If ``True``, the SmartCast app endpoints exist (TV-only on current
    firmware)."""

    keymap: Mapping[str, tuple[int, int]]
    """Remote key name → ``(codeset, code)``. Names not present in this
    map raise :class:`VizioUnsupportedError` from ``send_key``."""


class ChargingStatus(IntEnum):
    """Battery charging state for portable speakers."""

    NOT_CHARGING = 0
    CHARGING = 1
    FULLY_CHARGED = 2


class SettingType(StrEnum):
    """Schema kind of a setting, drives how options/value are interpreted."""

    INT = "T_VALUE_V1"
    LIST = "T_LIST_V1"
    LIST_X = "T_LIST_X_V1"
    SLIDER = "T_VALUE_ABS_V1"
    MENU = "T_MENU_V1"
    ACTION = "T_ACTION_V1"
    """A fire-able action item (e.g. ``system/timers/blank_screen``,
    ``admin_and_privacy/soft_power_cycle``). Carries no real value —
    the device reports the literal string ``"T_ACTION_V1"`` as VALUE.
    Trigger with :meth:`vizaio.Vizio.trigger_setting_action`."""


class AuthRequirement(StrEnum):
    """Whether an endpoint needs an auth token."""

    NONE = "none"
    """Endpoint always works without auth (e.g., ``/state/device/deviceinfo``)."""

    OPTIONAL = "optional"
    """Endpoint accepts auth if available; works without it."""

    REQUIRED = "required"
    """Endpoint refuses unauthenticated calls. Library raises
    :class:`VizioAuthError` before the request is sent if no token is set."""


class ResponseStatus(StrEnum):
    """Outcomes reported by the device in ``STATUS.RESULT``."""

    SUCCESS = "success"
    INVALID_PARAMETER = "invalid_parameter"
    URI_NOT_FOUND = "uri_not_found"
    """Modern firmware (~3.7+) returns this for paths the device doesn't
    expose. Mapped to :class:`VizioNotFoundError` so the multi-path
    endpoint fallback in :class:`SmartCastClient` can chain to alt paths."""

    HASHVAL_ERROR = "hashval_error"
    """Stale hashval on a setting/input PUT. Modern firmware
    distinguishes this from generic ``INVALID_PARAMETER``. Mapped to
    :class:`VizioInvalidParameterError` so the existing hashval-race
    retry path in :meth:`Vizio.set_setting` fires."""

    REQUIRES_PAIRING = "requires_pairing"
    PAIRING_DENIED = "pairing_denied"
    BLOCKED = "blocked"
    FAILURE = "failure"
    UNKNOWN = "unknown"
    """Used when the device returns a status string we don't recognize.
    The original string is preserved on the response object."""


@dataclass(frozen=True, slots=True)
class InputInfo:
    """A single input on the device."""

    name: str
    """Display label (e.g., ``"HDMI-1"``, ``"CAST"``)."""

    meta_name: str
    """User-customized name (e.g., ``"PS5"``, ``"Mac"``). Empty string
    if unset. For ``CAST`` the factory default meta_name is
    ``"SMARTCAST"`` — not actually user-set, but the device exposes
    it through this field."""

    is_current: bool
    """``True`` if this is the input currently selected on the device."""

    cname: str = ""
    """The device's canonical lowercase identifier (e.g., ``"hdmi1"``,
    ``"cast"``, ``"tuner"``). This is the **only** form the device
    accepts in a ``current_input`` PUT body — display name and
    meta_name are rejected. ``set_input`` accepts any of the three
    forms and translates to ``cname`` automatically."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Configuration tuple identifying a SmartCast app launch target."""

    app_id: str
    name_space: int
    message: str | None = None


@dataclass(frozen=True, slots=True)
class AppRecord:
    """An entry in the SmartCast app catalog (bundled + remote refresh)."""

    name: str
    country: tuple[str, ...]
    """ISO country codes the app is available in. ``("*",)`` means worldwide."""

    config: tuple[AppConfig, ...] = ()
    """Legacy launch configs from the pre-2025 catalog format. The current
    SmartCast catalog (``scfs.vizio.com``) returns metadata only, so this
    will be empty for entries fetched from the modern endpoint — launch
    payloads now come from :class:`AppAvailability` per-chipset/firmware.
    Retained for backward compatibility with archived catalogs."""

    id: str = ""
    """Catalog identifier, joins with :attr:`AppAvailability.app_id`.
    Empty when the catalog payload doesn't expose an id (older format)."""

    description: str = ""
    """Marketing copy from ``mobileAppInfo.description``. Empty when the
    catalog entry doesn't expose it."""

    icon_url: str = ""
    """Icon URL from ``mobileAppInfo.app_icon_image_url``. Empty when the
    catalog entry doesn't expose it."""


@dataclass(frozen=True, slots=True)
class ChipsetPayload:
    """
    One launch-payload variant within an :class:`AppAvailability` entry.

    A given app/chipset combination may have multiple variants (different
    firmware ranges shipping different launch configs). Pick the one whose
    ``firmware_minimum`` ≤ device firmware ≤ ``firmware_maximum``.
    """

    config: AppConfig
    """The launch payload — what gets PUT to ``/app/launch``."""

    firmware_minimum: str = ""
    """Lower firmware bound (inclusive), Vizio version string. Empty
    means unbounded below."""

    firmware_maximum: str = ""
    """Upper firmware bound (inclusive), Vizio version string. Empty
    means unbounded above."""


@dataclass(frozen=True, slots=True)
class AppAvailability:
    """
    Per-chipset/firmware launch metadata for one app.

    Joins with :class:`AppRecord` via :attr:`app_id` ↔ :attr:`AppRecord.id`.
    The chipset key ``"*"`` means "applies to all chipsets" — used as a
    fallback when no chipset-specific entry matches the device.
    """

    app_id: str
    """Catalog id (matches :attr:`AppRecord.id`)."""

    chipsets: Mapping[str, tuple[ChipsetPayload, ...]]
    """Chipset key → tuple of payload variants. Keys are availability
    chipset names (e.g., ``"MT5583"``, ``"NT72690"``) or ``"*"``.
    The library wraps the mapping in :class:`types.MappingProxyType`
    inside :func:`apps._parse_availability` so it's read-only at
    runtime; the type is :class:`Mapping` rather than ``dict`` to
    document that contract at the API boundary."""


@dataclass(frozen=True, slots=True)
class SettingInfo:
    """
    A single setting and its metadata.

    ``hashval`` is the opaque server-assigned identifier required for writes.
    Pass it to :meth:`Vizio.set_setting` to skip the redundant GET.
    """

    setting_type: str
    """Setting category, e.g., ``"audio"``, ``"picture"``."""

    name: str
    """Setting name, e.g., ``"volume"``, ``"mute"``."""

    value: int | str
    """Current value."""

    hashval: int
    """Server-assigned write token. Required to write this setting."""

    type: SettingType
    """Schema kind. Determines which of min/max/options apply."""

    min: int | None = None
    max: int | None = None
    center: int | None = None
    """Center value for SLIDER settings (e.g., EQ at 0)."""

    options: tuple[str, ...] = ()
    """Allowed values for LIST/LIST_X settings."""


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """A Vizio device located via network discovery."""

    name: str
    ip: str
    port: int
    """Primary HTTPS REST port (mDNS ``hsp`` field). The
    :class:`Vizio` constructor's ``host=`` argument expects this."""

    model: str
    id: str = ""

    @property
    def host(self) -> str:
        """Combine ``ip:port`` for use as a :class:`Vizio` constructor arg."""
        return f"{self.ip}:{self.port}"


@dataclass(frozen=True, slots=True)
class PairChallenge:
    """Result of starting a pairing handshake; contains data needed to complete."""

    challenge_type: int
    token: int


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Aggregate device identity. Returned by :meth:`Vizio.get_device_info`."""

    model: str = ""
    serial_number: str = ""
    esn: str = ""
    version: str = ""
    inputs: tuple[InputInfo, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class StateExtended:
    """
    Aggregate state snapshot from ``GET /state_extended``.

    Returns power, current app, current input, screen mode, and media
    state in **one** HTTP round trip — meaningfully cheaper than five
    individual GETs for HA-style polling integrations.

    Capability is advertised by the device under
    ``deviceinfo.scpl_capabilities.state_extended``. Older firmware
    that doesn't advertise the capability will raise
    :class:`VizioUnsupportedError` from :meth:`Vizio.get_state_extended`
    before any HTTP work.

    The on-the-wire envelope is **distinct** from the regular SCPL
    response shape — it has flat top-level keys and no ``STATUS`` /
    ``ITEMS`` wrapper. ``raw`` exposes the original parsed payload as
    an escape hatch for fields we don't model.
    """

    power_on: bool
    """Derived from ``POWER_STATUS.VALUE`` (``1 = on, 0 = off``)."""

    power_mode: str
    """Human-readable mode such as ``"On"``, ``"Active Off"``,
    ``"Quick Start"``. Empty string if the field is missing."""

    current_input: str
    """Meta-name of the active input (e.g., ``"SMARTCAST"``,
    ``"HDMI-1"``). Same value :meth:`Vizio.get_current_input` returns."""

    current_input_hashval: int | None
    """Hashval of the current_input setting — useful for
    :meth:`Vizio.set_input` callers that want to skip the GET."""

    current_app: AppConfig | None
    """Active SmartCast app config, or ``None`` when no app is running.
    For the SmartCast Home screen this is populated with
    ``app_id='1', name_space=4``."""

    screen_mode: str
    """e.g., ``"Full screen"``, ``"PIP"``."""

    media_state: str
    """Session-level playback indicator. Raw wire values are
    ``MediaState::``-prefixed (Vizio uses C++-style namespace prefixes);
    callers usually want ``.split("::", 1)[1]`` to get the bare value.
    Two values observed empirically (shown here in raw form):

    - ``"MediaState::Playing"`` — a media session is loaded.
    - ``"MediaState::Stopped"`` — no media session (SmartCast Home,
      app's own home screen, app exited, etc.).

    **This is session state, not transport state.** Pause, seek, ad
    interstitials, and seek-to-unloaded buffering all stay on
    ``MediaState::Playing`` — the field flips on "is a session
    loaded" not "are bytes advancing right now." Empirically verified
    against YouTube on a V-series TV; firmware 3.720.x. The official
    Vizio Android app doesn't deserialize this field at all (its
    ``ExtendedStateResponse`` model only carries ``POWER_STATUS``,
    ``APP_CURRENT``, ``CURRENT_INPUT``, ``DEVICE_NAME``, ``URI``,
    ``ERRORS``), so there is no canonical client-side enum to consult —
    the device's C++ enum may have additional values that no observed
    app surface triggers."""

    device_name: str
    """User-set TV name (e.g., ``"Test Living Room"``)."""

    errors: tuple[str, ...] = ()
    """Per-field errors reported alongside the snapshot. Empty in the
    common case."""

    raw: Mapping[str, Any] = field(default_factory=dict)
    """Original parsed JSON payload (case-preserved). Escape hatch for
    fields we don't model (firmware-specific extensions)."""


@dataclass(frozen=True, slots=True)
class SystemVersions:
    """
    Version/identity snapshot from ``GET /system/versions``.

    One round trip returns firmware, serial, ESN and the per-component
    version map (SCPL, ACR, AppleTV, …) — cleaner than the separate
    ``menu_native`` ``system_information`` scrapes for ESN / serial /
    version. The common fields are typed; everything the device reports
    (keys verbatim, e.g. ``"SERIAL NUMBER"``, ``"SC CONFIG"``) is on
    :attr:`raw`.
    """

    firmware: str = ""
    serial_number: str = ""
    esn: str = ""
    scpl: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)
    """Every key the device returns under ``ITEM.VALUE``, verbatim."""
