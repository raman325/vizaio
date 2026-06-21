"""Modern async Python client for Vizio SmartCast devices."""

from __future__ import annotations

from ._device import PairSession, Vizio
from ._keys import RemoteKey
from .apps import fetch_app_availability, fetch_app_catalog
from .discovery import (
    async_classify_device,
    async_is_tv,
    classify_crave_model,
    is_crave_model,
)
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
    AppAvailability,
    AppConfig,
    AppRecord,
    AuthRequirement,
    ChargingStatus,
    ChipsetPayload,
    DeviceInfo,
    DeviceProfile,
    DeviceType,
    DiscoveredDevice,
    InputInfo,
    PairChallenge,
    ResponseStatus,
    SettingInfo,
    SettingType,
    StateExtended,
)

__version__ = "0.1.0"

__all__ = [
    "CRAVE360_PROFILE",
    "CRAVE_GO_PROFILE",
    "CRAVE_PRO_PROFILE",
    "SOUNDBAR_PROFILE",
    "TV_PROFILE",
    "AppAvailability",
    "AppConfig",
    "AppRecord",
    "AuthRequirement",
    "ChargingStatus",
    "ChipsetPayload",
    "DeviceInfo",
    "DeviceProfile",
    "DeviceType",
    "DiscoveredDevice",
    "InputInfo",
    "PairChallenge",
    "PairSession",
    "RemoteKey",
    "ResponseStatus",
    "SettingInfo",
    "SettingType",
    "StateExtended",
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
    "async_classify_device",
    "async_is_tv",
    "classify_crave_model",
    "fetch_app_availability",
    "fetch_app_catalog",
    "is_crave_model",
]
