"""Modern async Python client for Vizio SmartCast devices."""

from __future__ import annotations

from ._device import PairSession, Vizio
from ._keys import RemoteKey
from ._websocket import EventStream, SubscribeOptions
from .errors import (
    VizioAuthError,
    VizioBusyError,
    VizioConnectionError,
    VizioError,
    VizioInvalidInputError,
    VizioInvalidParameterError,
    VizioNotFoundError,
    VizioResponseError,
    VizioUnsupportedError,
)
from .profiles import (
    CRAVE360_PROFILE,
    CRAVE_GO_PROFILE,
    CRAVE_PRO_PROFILE,
    SOUNDBAR_PROFILE,
    TV_PROFILE,
)
from .types import (
    AppConfig,
    AppRecord,
    AuthRequirement,
    ChargingStatus,
    DeviceInfo,
    DeviceProfile,
    DeviceType,
    DiscoveredDevice,
    InputInfo,
    PairChallenge,
    ResponseStatus,
    SettingInfo,
    SettingType,
    StateEvent,
    StateExtended,
)

__version__ = "0.1.0"

__all__ = [
    "CRAVE360_PROFILE",
    "CRAVE_GO_PROFILE",
    "CRAVE_PRO_PROFILE",
    "SOUNDBAR_PROFILE",
    "TV_PROFILE",
    "AppConfig",
    "AppRecord",
    "AuthRequirement",
    "ChargingStatus",
    "DeviceInfo",
    "DeviceProfile",
    "DeviceType",
    "DiscoveredDevice",
    "EventStream",
    "InputInfo",
    "PairChallenge",
    "PairSession",
    "RemoteKey",
    "ResponseStatus",
    "SettingInfo",
    "SettingType",
    "StateEvent",
    "StateExtended",
    "SubscribeOptions",
    "Vizio",
    "VizioAuthError",
    "VizioBusyError",
    "VizioConnectionError",
    "VizioError",
    "VizioInvalidInputError",
    "VizioInvalidParameterError",
    "VizioNotFoundError",
    "VizioResponseError",
    "VizioUnsupportedError",
]
