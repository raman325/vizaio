"""
Exception hierarchy for vizaio.

All exceptions inherit from :class:`VizioError`. Callers that want to treat
"any device problem" uniformly can catch :class:`VizioError`; callers that
need to distinguish (e.g., a Home Assistant config flow distinguishing auth
failures from connection failures) can catch the specific subclass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import WifiResult


class VizioError(Exception):
    """Base class for all vizaio exceptions."""


class VizioConnectionError(VizioError):
    """Could not reach the device, or transport-level failure (TCP/TLS/timeout)."""


class VizioAuthError(VizioError):
    """Auth token missing, invalid, or rejected by the device."""


class VizioResponseError(VizioError):
    """Device returned a malformed or unsuccessful response."""


class VizioInvalidParameterError(VizioError):
    """Device rejected the request as invalid (e.g., bad VALUE or HASHVAL)."""


class VizioInvalidInputError(VizioInvalidParameterError):
    """The named input does not exist on this device."""


class VizioNotFoundError(VizioError):
    """Expected item (e.g., a setting cname) was not present in the response."""


class VizioBusyError(VizioError):
    """
    Device is in a state that conflicts with the requested operation.

    Most commonly raised when a write fails with a stale hashval mid-race
    and we couldn't recover, or when the device is already in pairing mode
    when ``pair_session`` is entered.
    """


class VizioUnsupportedError(VizioError):
    """
    The requested operation is not supported by this device type.

    For example, ``get_battery_level`` on a ``DeviceType.TV``.
    """


class VizioWifiError(VizioError):
    """
    A Wi-Fi provisioning leaf reported a radio or DHCP failure.

    Carries the parsed :class:`vizaio.types.WifiResult` so callers can
    branch — an interactive client re-prompts for the password on
    ``AUTH_REJECTED`` rather than aborting — plus the raw device string
    for codes we don't model.
    """

    def __init__(self, result: WifiResult, code: str, detail: str = "") -> None:
        """Store the parsed result alongside the device's original string."""
        self.result = result
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)
