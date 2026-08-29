"""Modern async Python client for Vizio SmartCast devices."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from ._device import PairSession, Vizio, WifiSetupSession
from ._keys import RemoteKey
from .apispec import ApiSpec
from .apps import (
    fetch_app_availability,
    fetch_app_catalog,
    fetch_remote_app_catalog,
    is_app_input,
)
from .discovery import (
    async_classify_device,
    async_is_tv,
    async_resolve_host,
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
    VizioWifiError,
)
from .profiles import (
    CRAVE360_PROFILE,
    CRAVE_GO_PROFILE,
    CRAVE_PRO_PROFILE,
    SOUNDBAR_PROFILE,
    TV_PROFILE,
)
from .types import (
    AccessPoint,
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
    SystemVersions,
    WifiResult,
)

# Derived from the installed distribution metadata so it always matches the
# real release version. The release workflow stamps the version from the git
# tag into the built distribution, so a hardcoded string here would ship stale
# (e.g. the 0.2.0 wheel previously reported "0.1.0"). Falls back for an
# uninstalled source tree.
try:
    __version__ = _pkg_version("vizaio")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = [
    "CRAVE360_PROFILE",
    "CRAVE_GO_PROFILE",
    "CRAVE_PRO_PROFILE",
    "SOUNDBAR_PROFILE",
    "TV_PROFILE",
    "AccessPoint",
    "ApiSpec",
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
    "SystemVersions",
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
    "VizioWifiError",
    "WifiResult",
    "WifiSetupSession",
    "async_classify_device",
    "async_is_tv",
    "async_resolve_host",
    "classify_crave_model",
    "fetch_app_availability",
    "fetch_app_catalog",
    "fetch_remote_app_catalog",
    "is_app_input",
    "is_crave_model",
]
