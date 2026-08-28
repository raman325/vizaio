"""Soundbar Wi-Fi provisioning — types, payloads, parsing, device API, session."""

from __future__ import annotations

import pytest

from vizaio.types import AccessPoint, ResponseStatus, SettingType, WifiResult


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


def test_wifi_result_values_are_lowercase() -> None:
    # wire._parse_status lowercases before lookup; these must match that.
    for member in WifiResult:
        assert member.value == member.value.lower()


def test_wifi_result_covers_the_app_vocabulary() -> None:
    assert WifiResult("net_wifi_already_connected") is WifiResult.ALREADY_CONNECTED
    assert WifiResult("net_wifi_auth_rejected") is WifiResult.AUTH_REJECTED
    assert WifiResult("net_ip_dhcp_failed") is WifiResult.DHCP_FAILED


def test_requires_system_pin_is_a_response_status() -> None:
    assert ResponseStatus("requires_system_pin") is ResponseStatus.REQUIRES_SYSTEM_PIN


def test_network_setting_types_are_modelled() -> None:
    assert SettingType("T_APS_V1") is SettingType.ACCESS_POINTS
    assert SettingType("T_AP_V1") is SettingType.ACCESS_POINT
    assert SettingType("T_STRING_V1") is SettingType.STRING
    assert SettingType("T_TEST_CONNECTION_V1") is SettingType.TEST_CONNECTION
