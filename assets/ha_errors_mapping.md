# Mapping `vizio-smartcast` exceptions to Home Assistant exceptions

The exception hierarchy is designed so the HA `vizio` integration can map
errors cleanly without needing to inspect message text.

## Exception hierarchy

```
VizioError                        — base; catch this for "any device problem"
├── VizioConnectionError          — TCP/TLS/timeout, device unreachable
├── VizioAuthError                — token missing, invalid, or rejected
├── VizioResponseError            — malformed response from device
├── VizioInvalidParameterError    — device rejected as bad input (bad value/hashval)
│   └── VizioInvalidInputError    — input name doesn't exist on this device
├── VizioNotFoundError            — expected item missing from response
├── VizioBusyError                — device in conflicting state (rare)
└── VizioUnsupportedError         — operation not supported by this profile
```

## HA config flow mapping

```python
from homeassistant.config_entries import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from vizio_smartcast import (
    VizioAuthError,
    VizioConnectionError,
    VizioError,
    VizioInvalidParameterError,
    VizioResponseError,
)

async def _try_validate(host, auth_token, device_type):
    async with Vizio(
        host=host, device_type=device_type, auth_token=auth_token
    ) as v:
        try:
            await v.ping_auth()
        except VizioAuthError as e:
            raise ConfigEntryAuthFailed(str(e)) from e
        except VizioConnectionError as e:
            # Device unreachable — temporary, HA will retry
            raise ConfigEntryNotReady(str(e)) from e
        except VizioInvalidParameterError as e:
            # Bad config (e.g., wrong device_type) — permanent
            raise ConfigEntryError(str(e)) from e
        except VizioResponseError as e:
            # Device returned unexpected shape — likely a firmware bug,
            # treat as not-ready (HA will retry, possibly succeed)
            raise ConfigEntryNotReady(str(e)) from e
```

## DataUpdateCoordinator mapping

```python
from homeassistant.helpers.update_coordinator import UpdateFailed
from vizio_smartcast import VizioError

async def _async_update_data(self):
    try:
        return {
            "power": await self._vizio.get_power_state(),
            "volume": await self._vizio.get_volume(),
            "muted": await self._vizio.is_muted(),
            "current_input": await self._vizio.get_current_input(),
        }
    except VizioError as e:
        raise UpdateFailed(f"Vizio update failed: {e}") from e
```

For finer-grained handling (some metrics being unavailable shouldn't fail
the whole coordinator):

```python
async def _maybe(coro, default=None):
    try:
        return await coro
    except VizioError:
        return default

async def _async_update_data(self):
    return {
        "power": await _maybe(self._vizio.get_power_state(), default=False),
        "volume": await _maybe(self._vizio.get_volume()),
        "muted": await _maybe(self._vizio.is_muted()),
        "current_input": await _maybe(self._vizio.get_current_input()),
    }
```

## Service-call mapping

```python
from homeassistant.exceptions import HomeAssistantError

async def async_set_volume(self, level):
    try:
        await self._vizio.set_setting("audio", "volume", int(level))
    except VizioError as e:
        raise HomeAssistantError(f"Failed to set volume: {e}") from e
```

`VizioInvalidInputError` deserves a more specific surface — the user
typed an invalid input name in a service call:

```python
async def async_select_input(self, name):
    try:
        await self._vizio.set_input(name)
    except VizioInvalidInputError as e:
        # str(e) includes the valid input names
        raise HomeAssistantError(str(e)) from e
    except VizioError as e:
        raise HomeAssistantError(f"Input change failed: {e}") from e
```

## What about `VizioBusyError` and `VizioUnsupportedError`?

- **`VizioBusyError`** — device is in a conflicting state (e.g., already
  in pairing mode). HA should treat as `HomeAssistantError`. Rare.
- **`VizioUnsupportedError`** — operation not available on this device
  profile (e.g., `get_battery_level()` on a TV). Should not happen in
  HA — entity registration filters by capability. If raised, treat as
  programming error: log + `HomeAssistantError`.
