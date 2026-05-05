"""
SmartCast app catalog: bundled JSON + remote refresh with fallback.

The bundled catalog (``data/apps.json``) is shipped with the package as
the ground-truth fallback. The remote URL is what the SmartCast mobile
app fetches from — it goes down periodically, hence the fallback
strategy.

Public:

- :data:`APP_HOME` — the synthetic SmartCast Home screen entry
- :data:`BUNDLED_APPS` — frozen tuple of :class:`AppRecord` from the
  bundled JSON
- :data:`NO_APP_RUNNING` — sentinel returned by ``Vizio.get_current_app``
  when no app is active
- :func:`find_app_name` — resolves an :class:`AppConfig` against a catalog
- :func:`fetch_app_catalog` — async fetch with bundled fallback
"""

from __future__ import annotations

from collections.abc import Iterable
import json
import logging
from pathlib import Path
from typing import Any, Final

import aiohttp

from .types import AppConfig, AppRecord

_LOGGER = logging.getLogger(__name__)

REMOTE_CATALOG_URL: Final = (
    "https://hometest.buddytv.netdna-cdn.com/appservice/vizio_apps_prod.json"
)
"""URL the SmartCast mobile app fetches from. Periodically unreliable."""

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
)


def _load_bundled() -> tuple[AppRecord, ...]:
    """Read ``data/apps.json`` at import time."""
    data_path = Path(__file__).parent / "data" / "apps.json"
    if not data_path.exists():
        # Allow operation without a bundled catalog — find_app_name still
        # works for namespace 0 and exact-config matches a caller might
        # supply manually.
        _LOGGER.debug("No bundled apps.json at %s", data_path)
        return ()
    try:
        raw = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _LOGGER.warning("Failed to load bundled apps.json: %s", e)
        return ()
    return _parse_catalog(raw)


def _parse_catalog(raw: Any) -> tuple[AppRecord, ...]:
    """
    Parse a catalog payload into ``tuple[AppRecord, ...]``.

    Tolerant of the various shapes Vizio's catalog has historically had —
    the records may use either a list-of-configs or a single config dict.
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
        country_raw = entry.get("country", ["*"])
        country = (
            tuple(str(c) for c in country_raw)
            if isinstance(country_raw, list)
            else ("*",)
        )
        config_raw = entry.get("config", [])
        config_list: list[AppConfig] = []
        if isinstance(config_raw, dict):
            config_raw = [config_raw]
        if isinstance(config_raw, list):
            for c in config_raw:
                if not isinstance(c, dict):
                    continue
                app_id = c.get("APP_ID") or c.get("app_id")
                name_space = c.get("NAME_SPACE")
                if name_space is None:
                    name_space = c.get("name_space")
                message = c.get("MESSAGE") or c.get("message")
                if app_id is None or name_space is None:
                    continue
                config_list.append(
                    AppConfig(
                        app_id=str(app_id),
                        name_space=int(name_space),
                        message=str(message) if message is not None else None,
                    )
                )
        if not config_list:
            continue
        out.append(AppRecord(name=name, country=country, config=tuple(config_list)))
    return tuple(out)


BUNDLED_APPS: Final[tuple[AppRecord, ...]] = _load_bundled()


def find_app_name(config: AppConfig | None, apps: Iterable[AppRecord]) -> str | None:
    """
    Resolve an :class:`AppConfig` to an app name.

    - ``None`` → ``NO_APP_RUNNING`` sentinel
    - ``name_space == 0`` → ``APP_CAST`` (regardless of app_id; quirk #5)
    - exact match → app's ``name``
    - exact match with namespace 2↔4 swap → app's ``name`` (quirk #4)
    - no match → ``None``
    """
    if config is None:
        return NO_APP_RUNNING
    if config.name_space == 0:
        return APP_CAST
    for record in apps:
        for candidate in record.config:
            if _config_matches(config, candidate):
                return record.name
    return None


def _config_matches(a: AppConfig, b: AppConfig) -> bool:
    if a.app_id != b.app_id:
        return False
    if a.name_space == b.name_space:
        return True
    return (
        a.name_space in EQUIVALENT_NAME_SPACES
        and b.name_space in EQUIVALENT_NAME_SPACES
    )


async def fetch_app_catalog(
    session: aiohttp.ClientSession | None = None,
    *,
    timeout: float = 10.0,
) -> tuple[AppRecord, ...]:
    """
    Fetch the catalog from :data:`REMOTE_CATALOG_URL`.

    Falls back to :data:`BUNDLED_APPS` on ANY failure (connection error,
    timeout, malformed JSON, unexpected shape). Never raises — caller
    invariants assume "we always have a catalog."
    """
    own_session = session is None
    s = session or aiohttp.ClientSession()
    try:
        async with s.get(
            REMOTE_CATALOG_URL,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug(
                    "App catalog HTTP %s — using bundled fallback",
                    resp.status,
                )
                return BUNDLED_APPS
            text = await resp.text()
            payload = json.loads(text)
    except Exception as e:
        _LOGGER.debug("App catalog fetch failed (%s) — using bundled", e)
        return BUNDLED_APPS
    finally:
        if own_session:
            await s.close()

    parsed = _parse_catalog(payload)
    if not parsed:
        _LOGGER.debug("App catalog parsed empty — using bundled fallback")
        return BUNDLED_APPS
    return parsed
