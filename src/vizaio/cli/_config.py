"""
Persistent CLI config: device aliases, default device, auth tokens.

Storage: ``$VIZAIO_CONFIG`` if set, else
``platformdirs.user_config_dir("vizaio") / config.toml``.

Schema:

    default_device = "livingroom"

    [devices.livingroom]
    host = "192.168.1.50"
    device_type = "tv"
    auth_token = "..."

File mode: 0600 on Unix (best-effort) on first write.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
import logging
import os
from pathlib import Path
from typing import Self

import platformdirs
import tomlkit
from tomlkit.exceptions import ParseError

from ..types import DeviceType

_LOGGER = logging.getLogger(__name__)

_CONFIG_ENV = "VIZAIO_CONFIG"
_APP_NAME = "vizaio"
_CONFIG_FILENAME = "config.toml"


def default_config_path() -> Path:
    """Return the default config path, honoring the env var override."""
    env = os.environ.get(_CONFIG_ENV)
    if env:
        return Path(env)
    return Path(platformdirs.user_config_dir(_APP_NAME)) / _CONFIG_FILENAME


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    """A saved device alias."""

    name: str
    host: str
    device_type: DeviceType
    auth_token: str | None = None


@dataclass(slots=True)
class Config:
    """Mutable view of ``config.toml``. Call :meth:`save` to persist."""

    path: Path
    default_device: str | None = None
    _devices: dict[str, DeviceRecord] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Initialize the empty devices dict when one wasn't supplied."""
        if self._devices is None:
            self._devices = {}

    @classmethod
    def load(cls, path: Path | None = None) -> Self:
        """Read the config file, or return an empty in-memory config if missing."""
        path = path or default_config_path()
        if not path.exists():
            return cls(path=path)

        try:
            doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        except (OSError, ParseError) as e:
            _LOGGER.warning("Failed to parse %s: %s — starting empty", path, e)
            return cls(path=path)

        default = doc.get("default_device")
        devices_table = doc.get("devices") or {}
        devices: dict[str, DeviceRecord] = {}
        for name, body in devices_table.items():
            try:
                devices[str(name)] = DeviceRecord(
                    name=str(name),
                    host=str(body["host"]),
                    device_type=DeviceType(str(body["device_type"])),
                    auth_token=str(body["auth_token"])
                    if body.get("auth_token")
                    else None,
                )
            except (KeyError, ValueError) as e:
                _LOGGER.warning("Skipping malformed device %r: %s", name, e)

        return cls(
            path=path,
            default_device=str(default) if default else None,
            _devices=devices,
        )

    def save(self) -> None:
        """Persist to disk. Creates parent directory; sets 0600 on Unix."""
        doc = tomlkit.document()
        if self.default_device:
            doc["default_device"] = self.default_device

        devices_table = tomlkit.table()
        for name, record in sorted(self._devices.items()):
            entry = tomlkit.table()
            entry["host"] = record.host
            entry["device_type"] = record.device_type.value
            if record.auth_token:
                entry["auth_token"] = record.auth_token
            devices_table[name] = entry
        doc["devices"] = devices_table

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(tomlkit.dumps(doc), encoding="utf-8")

        # Best-effort 0600 on Unix-like systems. No-op on Windows.
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def add_device(self, record: DeviceRecord) -> None:
        """Insert or overwrite a device alias. Does not persist; call :meth:`save`."""
        self._devices[record.name] = record

    def remove_device(self, name: str) -> None:
        """Remove an alias (no-op if absent); clears default if it pointed there."""
        self._devices.pop(name, None)
        if self.default_device == name:
            self.default_device = None

    def get_device(self, name: str) -> DeviceRecord:
        """Return the alias's :class:`DeviceRecord`; raises ``KeyError`` if absent."""
        if name not in self._devices:
            raise KeyError(name)
        return self._devices[name]

    def list_devices(self) -> list[DeviceRecord]:
        """Return all device records sorted by alias name."""
        return sorted(self._devices.values(), key=lambda r: r.name)

    def __contains__(self, name: object) -> bool:
        """``name in config`` membership test against the alias keys."""
        return name in self._devices

    # Used by tests to build up a Config in-memory.
    def with_device(self, record: DeviceRecord) -> Self:
        """Add ``record`` and return ``self`` for fluent in-memory test setup."""
        self.add_device(record)
        return self

    # ---- updates -----------------------------------------------------

    def update_device(self, name: str, **fields: object) -> DeviceRecord:
        """Replace fields on an existing alias; raises ``KeyError`` if absent."""
        existing = self.get_device(name)
        new = replace(existing, **fields)  # type: ignore[arg-type]
        self._devices[name] = new
        return new
