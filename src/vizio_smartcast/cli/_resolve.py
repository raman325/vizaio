"""
Resolve --device / --host / config defaults into a usable spec.

Given the CLI's global flags and a loaded :class:`Config`, returns a
:class:`ResolvedDevice` describing how to instantiate :class:`Vizio`.

Resolution order:
1. ``--host IP[:PORT]`` ad-hoc (overrides take precedence for type/auth)
2. ``--device NAME`` alias from config (overrides override stored fields)
3. ``config.default_device`` if set
4. Otherwise raise — the user needs to be told they have no device target.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..types import DeviceType
from ._config import Config


@dataclass(frozen=True, slots=True)
class ResolvedDevice:
    host: str
    device_type: DeviceType
    auth_token: str | None


class CLIResolutionError(Exception):
    """Raised when the CLI can't pick a device from flags + config."""


def resolve_device(
    *,
    host: str | None,
    device_alias: str | None,
    device_type: DeviceType | None,
    auth_token: str | None,
    config: Config,
) -> ResolvedDevice:
    """Apply the resolution rules. Overrides win over config values."""
    if host:
        return ResolvedDevice(
            host=host,
            device_type=device_type or DeviceType.TV,
            auth_token=auth_token,
        )

    alias = device_alias or config.default_device
    if alias is None:
        raise CLIResolutionError(
            "No device specified. Pass --host IP, --device NAME, "
            "or set a default with `vizio-smartcast device set-default NAME`."
        )

    try:
        record = config.get_device(alias)
    except KeyError as e:
        raise CLIResolutionError(
            f"No device alias {alias!r}. Known: "
            f"{[r.name for r in config.list_devices()]}"
        ) from e

    return ResolvedDevice(
        host=record.host,
        device_type=device_type or record.device_type,
        auth_token=auth_token if auth_token is not None else record.auth_token,
    )
