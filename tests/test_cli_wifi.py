"""CLI tests for ``vizaio wifi``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from vizaio._device import WifiSetupSession
from vizaio.cli import app
from vizaio.errors import VizioWifiError
from vizaio.types import AccessPoint, WifiResult

runner = CliRunner()

_APS = (
    AccessPoint(ssid="HomeNet", bssid="aa", security="WPA2/PSK", band="5", rssi=65),
    AccessPoint(ssid="OpenNet", bssid="bb", security="NONE", band="2.4", rssi=40),
)


def _fake_device(**attrs: object) -> AsyncMock:
    """
    Build an AsyncMock standing in for a Vizio session.

    ``wifi_setup_session`` is deliberately wired to the *real*
    ``WifiSetupSession`` so the CLI tests exercise the actual
    start/stop bracketing rather than a stubbed-out context manager.
    It is also sync, so it must be a MagicMock — an AsyncMock attribute
    would hand back a coroutine.
    """
    device = AsyncMock()
    device.__aenter__.return_value = device
    device.get_access_points.return_value = _APS
    for key, value in attrs.items():
        setattr(device, key, value)
    device.wifi_setup_session = MagicMock(side_effect=lambda: WifiSetupSession(device))
    return device


def test_wifi_scan_lists_networks() -> None:
    device = _fake_device()
    resolver = AsyncMock(return_value="h:9000")
    with (
        patch("vizaio.cli.Vizio", return_value=device),
        patch("vizaio.cli.async_resolve_host", resolver),
    ):
        result = runner.invoke(app, ["wifi", "scan", "1.2.3.4"])
    assert result.exit_code == 0
    assert "HomeNet" in result.stdout
    assert "OpenNet" in result.stdout
    device.start_ap_scan.assert_awaited_once()
    device.stop_ap_scan.assert_awaited_once()
    # A bare IP must go through port probing (7345, then 9000).
    resolver.assert_awaited_once_with("1.2.3.4")


def test_wifi_join_passes_credentials() -> None:
    device = _fake_device()
    with (
        patch("vizaio.cli.Vizio", return_value=device),
        patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")),
    ):
        result = runner.invoke(
            app, ["wifi", "join", "1.2.3.4", "HomeNet", "--password", "pw"]
        )
    assert result.exit_code == 0
    device.join_access_point.assert_awaited_once_with(
        "HomeNet", password="pw", hidden=False
    )


def test_wifi_join_reports_a_rejected_password() -> None:
    device = _fake_device(
        join_access_point=AsyncMock(
            side_effect=VizioWifiError(
                WifiResult.AUTH_REJECTED, "NET_WIFI_AUTH_REJECTED"
            )
        )
    )
    with (
        patch("vizaio.cli.Vizio", return_value=device),
        patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")),
    ):
        result = runner.invoke(
            app, ["wifi", "join", "1.2.3.4", "HomeNet", "--password", "bad"]
        )
    assert result.exit_code == 1
    assert "NET_WIFI_AUTH_REJECTED" in result.output
