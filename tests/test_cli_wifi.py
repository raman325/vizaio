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


def test_interactive_selects_a_network_and_prompts_for_a_password() -> None:
    device = _fake_device()
    with (
        patch("vizaio.cli.Vizio", return_value=device),
        patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")),
    ):
        # "1" picks HomeNet (secured), then the password.
        result = runner.invoke(app, ["wifi", "interactive", "1.2.3.4"], input="1\npw\n")
    assert result.exit_code == 0
    device.join_access_point.assert_awaited_once_with(
        "HomeNet", password="pw", hidden=False
    )
    assert "vizaio discover" in result.stdout


def test_interactive_skips_the_password_prompt_for_an_open_network() -> None:
    device = _fake_device()
    with (
        patch("vizaio.cli.Vizio", return_value=device),
        patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")),
    ):
        # "2" picks OpenNet; no password line follows.
        result = runner.invoke(app, ["wifi", "interactive", "1.2.3.4"], input="2\n")
    assert result.exit_code == 0
    device.join_access_point.assert_awaited_once_with(
        "OpenNet", password=None, hidden=False
    )


def test_interactive_handles_a_hidden_network() -> None:
    device = _fake_device()
    with (
        patch("vizaio.cli.Vizio", return_value=device),
        patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")),
    ):
        result = runner.invoke(
            app, ["wifi", "interactive", "1.2.3.4"], input="0\nghost\npw\n"
        )
    assert result.exit_code == 0
    device.join_access_point.assert_awaited_once_with(
        "ghost", password="pw", hidden=True
    )


def test_interactive_reprompts_after_a_rejected_password() -> None:
    device = _fake_device(
        join_access_point=AsyncMock(
            side_effect=[
                VizioWifiError(WifiResult.AUTH_REJECTED, "NET_WIFI_AUTH_REJECTED"),
                None,
            ]
        )
    )
    with (
        patch("vizaio.cli.Vizio", return_value=device),
        patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")),
    ):
        result = runner.invoke(
            app, ["wifi", "interactive", "1.2.3.4"], input="1\nwrong\nright\n"
        )
    assert result.exit_code == 0
    assert device.join_access_point.await_count == 2


def test_interactive_rescans_when_nothing_is_found() -> None:
    device = _fake_device(get_access_points=AsyncMock(side_effect=[(), _APS]))
    with (
        patch("vizaio.cli.Vizio", return_value=device),
        patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")),
    ):
        # "y" retries the scan, then "1" picks HomeNet, then the password.
        result = runner.invoke(
            app, ["wifi", "interactive", "1.2.3.4"], input="y\n1\npw\n"
        )
    assert result.exit_code == 0
    assert device.get_access_points.await_count == 2
