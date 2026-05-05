"""
Exception hierarchy for vizio-smartcast.

All exceptions inherit from :class:`VizioError`. Callers that want to treat
"any device problem" uniformly can catch :class:`VizioError`; callers that
need to distinguish (e.g., a Home Assistant config flow distinguishing auth
failures from connection failures) can catch the specific subclass.
"""

from __future__ import annotations


class VizioError(Exception):
    """Base class for all vizio-smartcast exceptions."""


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
