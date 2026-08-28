"""Soundbar Wi-Fi provisioning — types, payloads, parsing, device API, session."""

from __future__ import annotations

import pytest

from vizaio.types import AccessPoint


def _ap(security: str) -> AccessPoint:
    """Build an AccessPoint varying only the security string."""
    return AccessPoint(
        ssid="net", bssid="aa:bb", security=security, band="2.4", rssi=50
    )


@pytest.mark.parametrize(
    ("security", "expected_open"),
    [
        ("NONE", True),
        ("WEP/NONE", True),
        ("WPA2/PSK", False),
        ("WPA/PSK", False),
        ("WEP", False),
        ("EAP", False),
        ("", True),
    ],
)
def test_access_point_is_open(security: str, expected_open: bool) -> None:
    assert _ap(security).is_open is expected_open


def test_access_point_is_frozen() -> None:
    with pytest.raises(AttributeError):
        _ap("NONE").ssid = "other"  # type: ignore[misc]
