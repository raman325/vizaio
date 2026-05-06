"""Reference DataUpdateCoordinator for the HA `vizio` integration.

Skeleton you can lift verbatim into ``homeassistant/components/vizio/``
when migrating from ``pyvizio``. NOT a runtime dependency — just docs.

Demonstrates:
- Lifecycle: ``Vizio`` instance created in ``__init__``, session managed
  by HA's aiohttp client (passed via ``session=`` kwarg).
- Hashval caching: poll ``get_settings("audio")`` once, write multiple
  audio settings using cached hashvals — no per-write GET.
- Pairing: configured via the config flow, not the coordinator.
- Exception → HA-exception mapping.
- Per-attribute defaults so a transient failure on one metric doesn't
  blank the whole entity.

Tested against:
- vizaio 0.1+
- Home Assistant 2026.x

If this file falls out of date, the test_device.py suite is the source
of truth for vizaio's behavior.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from vizaio import (
    DeviceType,
    SettingInfo,
    Vizio,
    VizioAuthError,
    VizioConnectionError,
    VizioError,
    VizioInvalidParameterError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL = timedelta(seconds=10)
"""Per protocol-notes #20, sub-second polling can saturate the device.
10s is a reasonable default; HA users can override."""


class VizioCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls a Vizio device, exposes shaped data to platform entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"vizio_{entry.entry_id}",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self._entry = entry
        self._vizio = Vizio(
            host=entry.data["host"],
            device_type=DeviceType(entry.data["device_type"]),
            auth_token=entry.data.get("auth_token"),
            session=async_get_clientsession(hass),
        )
        self._cached_audio_settings: dict[str, SettingInfo] = {}

    async def _async_setup(self) -> None:
        """First-time validation: confirm we can reach + auth the device.

        Called by HA's coordinator framework once before the first update.
        """
        try:
            await self._vizio.ping_auth()
        except VizioAuthError as e:
            raise ConfigEntryAuthFailed(str(e)) from e
        except VizioConnectionError as e:
            raise ConfigEntryNotReady(str(e)) from e
        except VizioInvalidParameterError as e:
            raise ConfigEntryError(str(e)) from e

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll once. Each metric defaults to None on failure rather than
        failing the whole update — UI can show 'unknown' per attribute."""

        async def _maybe(coro: Any, default: Any = None) -> Any:
            try:
                return await coro
            except VizioError as e:
                _LOGGER.debug("vizio: metric unavailable: %s", e)
                return default

        # Update audio-settings cache periodically. The hashvals here let
        # us write settings (volume, mute, EQ) without an extra GET each
        # time — see protocol-notes #17 and #13.
        try:
            self._cached_audio_settings = await self._vizio.get_settings("audio")
        except VizioError as e:
            _LOGGER.debug("vizio: audio settings refresh failed: %s", e)
            # Keep the previous cache; better stale than empty.

        return {
            "power": await _maybe(self._vizio.get_power_state(), default=False),
            "volume": await _maybe(self._vizio.get_volume()),
            "muted": await _maybe(self._vizio.is_muted()),
            "current_input": await _maybe(self._vizio.get_current_input()),
            "current_app": await _maybe(self._vizio.get_current_app()),
            "audio_settings": self._cached_audio_settings,
        }

    # ----- Service-call helpers ----------------------------------------

    async def async_set_volume(self, level: int) -> None:
        """Set volume using the cached hashval (skips the per-write GET).

        If the cached hashval is stale, vizaio retries with a
        fresh GET internally — caller sees no exception unless both
        attempts fail.
        """
        cached = self._cached_audio_settings.get("volume")
        try:
            await self._vizio.set_setting(
                "audio",
                "volume",
                int(level),
                hashval=cached.hashval if cached else None,
            )
        except VizioError as e:
            raise UpdateFailed(f"set_volume failed: {e}") from e

    async def async_set_input(self, name: str) -> None:
        try:
            await self._vizio.set_input(name)
        except VizioError as e:
            raise UpdateFailed(f"set_input failed: {e}") from e

    async def async_send_key(self, key: str) -> None:
        try:
            await self._vizio.send_key(key)
        except VizioError as e:
            raise UpdateFailed(f"send_key failed: {e}") from e

    async def async_close(self) -> None:
        """Called from HA's async_unload_entry."""
        await self._vizio.aclose()


# ---------------------------------------------------------------------
# Config flow snippet — for completeness
# ---------------------------------------------------------------------


async def async_pair_device(
    hass: HomeAssistant,
    host: str,
    device_type: DeviceType,
    pin_provider: Any,  # callable: () -> Awaitable[str]
) -> str:
    """Run the pairing flow and return the resulting auth token.

    ``pin_provider`` is whatever the config flow uses to prompt the user
    (typically an async function that opens a step in the UI and returns
    the user's PIN entry).
    """
    async with (
        Vizio(
            host=host,
            device_type=device_type,
            session=async_get_clientsession(hass),
        ) as v,
        v.pair_session(
            device_id="homeassistant", device_name="Home Assistant"
        ) as session,
    ):
        _LOGGER.debug(
            "vizio pairing: challenge_type=%d token=%d",
            session.challenge.challenge_type,
            session.challenge.token,
        )
        pin = await pin_provider()
        return await session.complete(pin=pin)
