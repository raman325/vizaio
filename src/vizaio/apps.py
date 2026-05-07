"""
SmartCast app catalog and per-chipset availability data.

Two data sources, both sharing a common ``id`` field:

- ``data/apps.json`` — catalog metadata (name, country, description, icon).
  Source: ``scfs.vizio.com/appservice/vizio_apps_prod.json``. Bundled as
  the offline fallback for the remote URL.
- ``data/app_availability.json`` — per-chipset/firmware launch payloads.
  Source: ``scfs.vizio.com/appservice/app_availability_prod.json``.
  Also bundled as offline fallback even though it changes more
  frequently — without it, ``launch_app`` cannot resolve a launch
  payload for an app discovered on the device.

Public:

- :data:`APP_HOME` — the synthetic SmartCast Home screen entry
- :data:`BUNDLED_APPS` / :data:`BUNDLED_AVAILABILITY`
- :data:`NO_APP_RUNNING` — sentinel returned by ``Vizio.get_current_app``
  when no app is active
- :func:`find_app_name` — resolves an :class:`AppConfig` against catalog +
  availability
- :func:`fetch_app_catalog` / :func:`fetch_app_availability` — async fetch
  with bundled fallback
- :func:`extract_chipset` — derive the availability chipset key from the
  device's ``BINARIES.ViziOS`` string
- :func:`pick_chipset_payload` — choose the right :class:`ChipsetPayload`
  for a given chipset + firmware
"""

from __future__ import annotations

from collections.abc import Iterable
import json
import logging
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Final

import aiohttp

from .types import AppAvailability, AppConfig, AppRecord, ChipsetPayload

_LOGGER = logging.getLogger(__name__)

REMOTE_CATALOG_URL: Final = "https://scfs.vizio.com/appservice/vizio_apps_prod.json"
"""URL the SmartCast mobile app fetches from. Periodically unreliable."""

REMOTE_AVAILABILITY_URL: Final = (
    "https://scfs.vizio.com/appservice/app_availability_prod.json"
)
"""Per-chipset/firmware launch payload + availability data."""

NO_APP_RUNNING: Final = "_NO_APP_RUNNING"
"""Sentinel string returned by ``Vizio.get_current_app`` when nothing is
running. Migration: pyvizio used the same string."""

UNKNOWN_APP: Final = "_UNKNOWN_APP"
"""Sentinel for an app whose config doesn't match anything in the catalog."""

APP_CAST: Final = "Cast"
"""Per-protocol-notes quirk #5: NAME_SPACE=0 is always the Cast/Home
screen, regardless of APP_ID."""

EQUIVALENT_NAME_SPACES: Final = (2, 4)
"""Per-protocol-notes quirk #4: namespaces 2 and 4 are interchangeable.

Documented behavior for app matching across firmware versions —
pyvizio commit 219260d (PR #97). Greppable so future-Vizio additions are
discoverable.
"""

CHIPSET_WILDCARD: Final = "*"
"""Availability key meaning "applies to every chipset" — used as fallback
when no chipset-specific entry matches."""

APP_HOME: Final = AppRecord(
    name="SmartCast Home",
    country=("*",),
    config=(
        AppConfig(
            app_id="1",
            name_space=4,
            message="http://127.0.0.1:12345/scfs/sctv/main.html",
        ),
    ),
    id="0",
)


# ---------------------------------------------------------------------------
# Catalog parsing (apps.json)
# ---------------------------------------------------------------------------


def _parse_catalog(raw: Any) -> tuple[AppRecord, ...]:
    """
    Parse a catalog payload into ``tuple[AppRecord, ...]``.

    Tolerates both shapes Vizio's catalog has shipped:

    - **Modern (``scfs.vizio.com``)**: metadata only — ``name``, ``id``,
      ``country``, ``mobileAppInfo``. No launch ``config`` field. Launch
      payloads come from :class:`AppAvailability` at lookup time.
    - **Legacy (``hometest.buddytv.netdna-cdn.com``)**: includes a
      ``config`` array with ``NAME_SPACE``/``APP_ID``/``MESSAGE``.
      Retained so archived dumps still parse.
    """
    if not isinstance(raw, list):
        return ()
    out: list[AppRecord] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        country = _parse_country(entry.get("country"))
        config = _parse_legacy_config(entry.get("config"))
        app_id = _coerce_id(entry.get("id"))
        mobile_info = entry.get("mobileAppInfo") or {}
        description = (
            str(mobile_info.get("description", ""))
            if isinstance(mobile_info, dict)
            else ""
        )
        icon_url = (
            str(mobile_info.get("app_icon_image_url", ""))
            if isinstance(mobile_info, dict)
            else ""
        )
        out.append(
            AppRecord(
                name=name,
                country=country,
                config=config,
                id=app_id,
                description=description,
                icon_url=icon_url,
            )
        )
    return tuple(out)


def _parse_country(raw: Any) -> tuple[str, ...]:
    """Coerce a catalog ``country`` value to a tuple; default ``("*",)`` worldwide."""
    if isinstance(raw, list):
        return tuple(str(c) for c in raw)
    return ("*",)


def _coerce_id(raw: Any) -> str:
    """
    Normalize the catalog id field.

    The legacy format wraps ids in a single-element list (``"id": ["162"]``);
    the modern format uses a bare string (``"id": "44"``). Both reduce to
    the same string here.
    """
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if raw is None:
        return ""
    return str(raw)


def _parse_legacy_config(raw: Any) -> tuple[AppConfig, ...]:
    """Parse the legacy ``config`` array; tolerate missing/malformed entries."""
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    out: list[AppConfig] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        app_id = c.get("APP_ID") or c.get("app_id")
        name_space = c.get("NAME_SPACE")
        if name_space is None:
            name_space = c.get("name_space")
        if app_id is None or name_space is None:
            continue
        message = c.get("MESSAGE") or c.get("message")
        out.append(
            AppConfig(
                app_id=str(app_id),
                name_space=int(name_space),
                message=str(message) if message is not None else None,
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Availability parsing (app_availability.json)
# ---------------------------------------------------------------------------


def _parse_availability(raw: Any) -> tuple[AppAvailability, ...]:
    """
    Parse an availability payload into ``tuple[AppAvailability, ...]``.

    Skips entries with ``id == null`` (the live endpoint returns 2 such
    entries that can't be joined to the catalog) and any malformed
    chipset payload.
    """
    if not isinstance(raw, list):
        return ()
    out: list[AppAvailability] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        app_id = entry.get("id")
        if app_id is None:
            continue
        chipsets_raw = entry.get("chipsets")
        if not isinstance(chipsets_raw, dict):
            continue
        chipsets: dict[str, tuple[ChipsetPayload, ...]] = {}
        for chip_key, payloads_raw in chipsets_raw.items():
            if not isinstance(payloads_raw, list):
                continue
            payloads = tuple(
                p for p in (_parse_payload(item) for item in payloads_raw) if p
            )
            if payloads:
                chipsets[str(chip_key)] = payloads
        if not chipsets:
            continue
        out.append(
            AppAvailability(
                app_id=str(app_id),
                chipsets=MappingProxyType(chipsets),
            )
        )
    return tuple(out)


def _parse_payload(raw: Any) -> ChipsetPayload | None:
    """
    Parse one ``chipsets[<key>][i]`` entry.

    The launch payload is itself JSON-encoded inside the
    ``app_type_payload`` string — Vizio's encoding choice, not ours.
    """
    if not isinstance(raw, dict):
        return None
    payload_str = raw.get("app_type_payload")
    if not isinstance(payload_str, str):
        return None
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    app_id = payload.get("APP_ID")
    name_space = payload.get("NAME_SPACE")
    if app_id is None or name_space is None:
        return None
    message = payload.get("MESSAGE")
    config = AppConfig(
        app_id=str(app_id),
        name_space=int(name_space),
        message=str(message) if message is not None else None,
    )
    return ChipsetPayload(
        config=config,
        firmware_minimum=str(raw.get("firmwareMinimum") or ""),
        firmware_maximum=str(raw.get("firmwareMaximum") or ""),
    )


# ---------------------------------------------------------------------------
# Bundle loading
# ---------------------------------------------------------------------------


def _load_bundled_json(filename: str) -> Any:
    """Load a JSON file from ``vizaio/data/``; ``None`` if missing or unparseable."""
    data_path = Path(__file__).parent / "data" / filename
    if not data_path.exists():
        _LOGGER.debug("No bundled %s at %s", filename, data_path)
        return None
    try:
        return json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _LOGGER.warning("Failed to load bundled %s: %s", filename, e)
        return None


def _load_bundled() -> tuple[AppRecord, ...]:
    """
    Load the bundled ``data/apps.json`` from disk at this module's location.

    A separate function (rather than a module-level constant build) so
    tests can monkeypatch ``__file__`` and rerun loading against
    fixtures.
    """
    return _parse_catalog(_load_bundled_json("apps.json") or [])


def _load_bundled_availability() -> tuple[AppAvailability, ...]:
    """Load the bundled ``data/app_availability.json`` from disk."""
    return _parse_availability(_load_bundled_json("app_availability.json") or [])


BUNDLED_APPS: Final[tuple[AppRecord, ...]] = _load_bundled()

BUNDLED_AVAILABILITY: Final[tuple[AppAvailability, ...]] = _load_bundled_availability()


# ---------------------------------------------------------------------------
# Chipset extraction + firmware comparison
# ---------------------------------------------------------------------------


_VIZIOOS_CHIPSET_RE: Final = re.compile(r"^(?:mtk|ntv?)(\d+)", re.IGNORECASE)
"""Match the chipset identifier prefix in a ``BINARIES.ViziOS`` string.

Examples accepted: ``mtk5583-…`` (MediaTek), ``nt72690-…`` /
``ntv72690-…`` (Novatek). Captures the numeric portion only — the
prefix maps to ``MT`` / ``NT`` for the availability key.
"""


def extract_chipset(
    vizios_binary: str | None,
    *,
    available_keys: Iterable[str] = (),
) -> str | None:
    """
    Derive an availability chipset key from a ``BINARIES.ViziOS`` string.

    Example: ``"mtk5583-1.6.1312.1"`` → ``"MT5583"``.

    The ``MT5586``/``MT5586L`` ambiguity (the binary string carries no
    suffix) is resolved by preferring the unsuffixed key when both
    appear in ``available_keys``; if only the L-suffixed variant
    exists, that's returned. ``available_keys`` should be the set of
    chipset keys observed in the current availability data so missing
    keys don't get falsely returned.

    Returns ``None`` when the input is empty or the prefix doesn't match
    a known SoC vendor.
    """
    if not vizios_binary:
        return None
    match = _VIZIOOS_CHIPSET_RE.match(vizios_binary)
    if not match:
        return None
    digits = match.group(1)
    prefix = "MT" if vizios_binary.lower().startswith("mt") else "NT"
    candidate = f"{prefix}{digits}"
    keys = set(available_keys)
    if not keys:
        return candidate
    if candidate in keys:
        return candidate
    suffixed = f"{candidate}L"
    if suffixed in keys:
        return suffixed
    return None


def _firmware_tuple(version: str) -> tuple[int, ...] | None:
    """
    Convert ``"3.720.9.1-1"`` → ``(3, 720, 9, 1, 1)``.

    Returns ``None`` if any component fails to parse — caller decides the
    policy. Empty string returns an empty tuple, which compares less
    than any populated bound (used as the "no min/max" sentinel).
    """
    if not version:
        return ()
    parts = re.split(r"[.\-]", version)
    try:
        return tuple(int(p) for p in parts if p)
    except ValueError:
        return None


def firmware_in_range(current: str, *, minimum: str = "", maximum: str = "") -> bool:
    """
    Return ``True`` iff ``current`` falls within ``[minimum, maximum]``.

    Empty bounds are unbounded. *Permissive* policy applies in three
    "unknown" cases — we'd rather show an app that might not work than
    hide one that would:

    - ``current`` is empty (older firmware that doesn't expose
      ``SYSTEM_INFO.VERSION``, or caller passes ``""`` to skip the
      firmware check) — bounds are not enforced.
    - any version (current/min/max) fails to parse as a tuple of ints
      — bounds are not enforced.
    """
    if not current:
        return True
    cur = _firmware_tuple(current)
    if cur is None:
        return True
    lo = _firmware_tuple(minimum)
    if lo is None:
        return True
    hi = _firmware_tuple(maximum)
    if hi is None:
        return True
    if minimum and cur < lo:
        return False
    return not (maximum and cur > hi)


def pick_chipset_payload(
    availability: AppAvailability,
    *,
    chipset: str | None,
    firmware: str = "",
) -> ChipsetPayload | None:
    """
    Pick the best :class:`ChipsetPayload` for a device.

    Resolution order:

    1. Chipset-specific entry whose firmware bounds include ``firmware``.
    2. Wildcard (``"*"``) entry whose firmware bounds include ``firmware``.

    Returns ``None`` when neither yields a match (the app isn't available
    on this chipset/firmware combination).
    """
    for key in (chipset, CHIPSET_WILDCARD):
        if not key:
            continue
        for payload in availability.chipsets.get(key, ()):
            if firmware_in_range(
                firmware,
                minimum=payload.firmware_minimum,
                maximum=payload.firmware_maximum,
            ):
                return payload
    return None


# ---------------------------------------------------------------------------
# Catalog ↔ availability lookups
# ---------------------------------------------------------------------------


def find_app_name(
    config: AppConfig | None,
    apps: Iterable[AppRecord],
    *,
    availability: Iterable[AppAvailability] = (),
) -> str | None:
    """
    Resolve an :class:`AppConfig` to an app name.

    - ``None`` → ``NO_APP_RUNNING`` sentinel
    - ``name_space == 0`` → ``APP_CAST`` (regardless of app_id; quirk #5)
    - exact match against any record's legacy ``config`` → record's name
    - exact match with namespace 2↔4 swap → record's name (quirk #4)
    - if ``availability`` is supplied, attempt reverse lookup: find the
      availability entry whose payload matches ``config``, then look up
      the catalog record with the same ``id``
    - no match → ``None``
    """
    if config is None:
        return NO_APP_RUNNING
    if config.name_space == 0:
        return APP_CAST

    apps_list = list(apps)
    for record in apps_list:
        for candidate in record.config:
            if _config_matches(config, candidate):
                return record.name

    matched_id = _availability_id_for_config(config, availability)
    if matched_id is None:
        return None
    for record in apps_list:
        if record.id == matched_id:
            return record.name
    return None


def find_app_record(name: str, apps: Iterable[AppRecord]) -> AppRecord | None:
    """Find a catalog record by case-insensitive name."""
    target = name.lower()
    for record in apps:
        if record.name.lower() == target:
            return record
    return None


def find_availability(
    app_id: str, availability: Iterable[AppAvailability]
) -> AppAvailability | None:
    """Find an availability entry by id."""
    for entry in availability:
        if entry.app_id == app_id:
            return entry
    return None


def _availability_id_for_config(
    config: AppConfig, availability: Iterable[AppAvailability]
) -> str | None:
    """
    Return the availability ``app_id`` whose payload matches ``config``.

    Searches every chipset variant — the device may be on a chipset whose
    entry matches even though another wouldn't. Used by
    :func:`find_app_name` to identify apps without a legacy ``config``.
    """
    for entry in availability:
        for payloads in entry.chipsets.values():
            for payload in payloads:
                if _config_matches(config, payload.config):
                    return entry.app_id
    return None


def _config_matches(a: AppConfig, b: AppConfig) -> bool:
    """Compare :class:`AppConfig` for catalog identity (NAME_SPACE 2/4 equivalent)."""
    if a.app_id != b.app_id:
        return False
    if a.name_space == b.name_space:
        return True
    return (
        a.name_space in EQUIVALENT_NAME_SPACES
        and b.name_space in EQUIVALENT_NAME_SPACES
    )


# ---------------------------------------------------------------------------
# Remote fetches
# ---------------------------------------------------------------------------


async def fetch_app_catalog(
    session: aiohttp.ClientSession | None = None,
    *,
    timeout: float = 10.0,
    url: str = REMOTE_CATALOG_URL,
) -> tuple[AppRecord, ...]:
    """
    Fetch the catalog from ``url`` (defaults to :data:`REMOTE_CATALOG_URL`).

    Pass a different ``url`` to point at a regional mirror, an internal
    proxy, or a test fixture — the parser is the same regardless of
    source. Falls back to :data:`BUNDLED_APPS` on ANY failure (connection
    error, timeout, malformed JSON, unexpected shape). Never raises —
    caller invariants assume "we always have a catalog."
    """
    payload = await _fetch_json(url, session, timeout, "catalog")
    if payload is None:
        return BUNDLED_APPS
    parsed = _parse_catalog(payload)
    if not parsed:
        _LOGGER.debug("App catalog parsed empty — using bundled fallback")
        return BUNDLED_APPS
    return parsed


async def fetch_app_availability(
    session: aiohttp.ClientSession | None = None,
    *,
    timeout: float = 10.0,
    url: str = REMOTE_AVAILABILITY_URL,
) -> tuple[AppAvailability, ...]:
    """
    Fetch availability data from ``url``.

    Defaults to :data:`REMOTE_AVAILABILITY_URL`. Pass a different
    ``url`` to point at a regional mirror, an internal
    proxy, or a test fixture. Falls back to :data:`BUNDLED_AVAILABILITY`
    on any failure. Never raises. The bundled snapshot drifts from
    upstream (Vizio rolls availability forward with firmware), so callers
    should prefer fresh fetches at session start.
    """
    payload = await _fetch_json(url, session, timeout, "availability")
    if payload is None:
        return BUNDLED_AVAILABILITY
    parsed = _parse_availability(payload)
    if not parsed:
        _LOGGER.debug("App availability parsed empty — using bundled fallback")
        return BUNDLED_AVAILABILITY
    return parsed


async def _fetch_json(
    url: str,
    session: aiohttp.ClientSession | None,
    timeout: float,
    label: str,
) -> Any:
    """GET ``url`` and parse JSON; swallow errors to ``None`` (bundled fallback)."""
    own_session = session is None
    s = session or aiohttp.ClientSession()
    try:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                _LOGGER.debug(
                    "App %s HTTP %s — using bundled fallback", label, resp.status
                )
                return None
            text = await resp.text()
            return json.loads(text)
    except Exception as e:
        _LOGGER.debug("App %s fetch failed (%s) — using bundled", label, e)
        return None
    finally:
        if own_session:
            await s.close()


# ---------------------------------------------------------------------------
# Country filter
# ---------------------------------------------------------------------------


def app_in_country(record: AppRecord, country: str | None) -> bool:
    """
    Return ``True`` if ``record`` is available in ``country``.

    Wildcards: a record with ``country == ("*",)`` is available everywhere.
    Comparison is case-insensitive (the catalog uses ``"USA"`` while
    older entries used ``"usa"``). Passing ``None`` disables filtering.
    """
    if country is None:
        return True
    if any(c == "*" for c in record.country):
        return True
    target = country.lower()
    return any(c.lower() == target for c in record.country)
