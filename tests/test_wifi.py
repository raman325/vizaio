"""Soundbar Wi-Fi provisioning — types, payloads, parsing, device API, session."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from vizaio import DeviceType, Vizio
from vizaio._device import WifiSetupSession
from vizaio.client import _check_status
from vizaio.endpoints import EndpointSpec
from vizaio.errors import (
    VizioAuthError,
    VizioError,
    VizioResponseError,
    VizioWifiError,
)
from vizaio.parse import parse_access_points, parse_current_access_point
from vizaio.types import (
    AccessPoint,
    AuthRequirement,
    ResponseStatus,
    SettingType,
    WifiResult,
)
from vizaio.wire import Response


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


_SPEC = EndpointSpec(paths=("/x",), method="PUT", auth=AuthRequirement.NONE)


def _status_response(result: str) -> Response:
    """Build a Response carrying an arbitrary STATUS.RESULT string."""
    return Response.from_json({"STATUS": {"RESULT": result, "DETAIL": "d"}})


def test_wifi_result_maps_to_wifi_error() -> None:
    with pytest.raises(VizioWifiError) as excinfo:
        _check_status(_status_response("NET_WIFI_AUTH_REJECTED"), _SPEC)
    assert excinfo.value.result is WifiResult.AUTH_REJECTED
    assert excinfo.value.code == "NET_WIFI_AUTH_REJECTED"


def test_already_connected_still_raises_at_the_transport_layer() -> None:
    # The tolerance for ALREADY_CONNECTED lives in join_access_point, not
    # here — the transport layer has no idea which flow it is serving.
    with pytest.raises(VizioWifiError) as excinfo:
        _check_status(_status_response("NET_WIFI_ALREADY_CONNECTED"), _SPEC)
    assert excinfo.value.result is WifiResult.ALREADY_CONNECTED


def test_unmodelled_net_code_maps_to_unknown_but_keeps_raw() -> None:
    with pytest.raises(VizioWifiError) as excinfo:
        _check_status(_status_response("NET_WIFI_SOMETHING_NEW"), _SPEC)
    assert excinfo.value.result is WifiResult.UNKNOWN
    assert excinfo.value.code == "NET_WIFI_SOMETHING_NEW"


def test_requires_system_pin_maps_to_auth_error() -> None:
    with pytest.raises(VizioAuthError):
        _check_status(_status_response("REQUIRES_SYSTEM_PIN"), _SPEC)


def test_wifi_error_is_a_vizio_error() -> None:
    assert issubclass(VizioWifiError, VizioError)


def _aps_response() -> Response:
    """The real three-AP payload from issue #40, SSIDs redacted."""
    return Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {
                    "HASHVAL": 203850784,
                    "CNAME": "wireless_access_points",
                    "TYPE": "T_APS_V1",
                    "NAME": "Wireless Access Points",
                    "VALUE": [
                        {
                            "EM": "WPA2/PSK",
                            "RSSI": 65,
                            "NAME": "net-5g",
                            "BSSID": "aa:bb:cc",
                            "BAND": "5",
                        },
                        {
                            "EM": "WPA2/PSK",
                            "RSSI": 70,
                            "NAME": "net-24",
                            "BSSID": "dd:ee:ff",
                            "BAND": "2.4",
                        },
                    ],
                }
            ],
        }
    )


def test_parse_access_points_reads_the_scan_list() -> None:
    aps = parse_access_points(_aps_response())
    assert [a.ssid for a in aps] == ["net-5g", "net-24"]
    assert aps[0].band == "5"
    assert aps[0].rssi == 65
    assert aps[0].security == "WPA2/PSK"
    assert aps[0].bssid == "aa:bb:cc"
    assert aps[0].is_open is False


def test_parse_access_points_empty_when_item_absent() -> None:
    response = Response.from_json({"STATUS": {"RESULT": "SUCCESS", "DETAIL": ""}})
    assert parse_access_points(response) == ()


def test_parse_current_access_point_returns_none_when_unconfigured() -> None:
    # Real sentinel from issue #40: empty NAME, zeroed BSSID.
    response = Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {
                    "HASHVAL": 3250072061,
                    "CNAME": "current_access_point",
                    "TYPE": "T_AP_V1",
                    "NAME": "Current Access Point",
                    "VALUE": [
                        {
                            "EM": "NONE",
                            "RSSI": 0,
                            "NAME": "",
                            "BSSID": "000000-000000",
                            "BAND": "2.4",
                        }
                    ],
                }
            ],
        }
    )
    assert parse_current_access_point(response) is None


def test_parse_current_access_point_returns_the_joined_network() -> None:
    response = Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {
                    "CNAME": "current_access_point",
                    "TYPE": "T_AP_V1",
                    "NAME": "Current Access Point",
                    "VALUE": [
                        {
                            "EM": "WPA2/PSK",
                            "RSSI": 62,
                            "NAME": "joined",
                            "BSSID": "aa:bb",
                            "BAND": "5",
                        }
                    ],
                }
            ],
        }
    )
    ap = parse_current_access_point(response)
    assert ap is not None
    assert ap.ssid == "joined"


def _soundbar(client: Any) -> Vizio:
    """Build a Vizio bound to a stub client, bypassing HTTP entirely."""
    device = Vizio(host="192.168.1.101:9000", device_type=DeviceType.SOUNDBAR)
    device._client = client  # noqa: SLF001
    return device


def _leaf_response(cname: str, hashval: int, value: Any = "") -> Response:
    """Build a single-item GET response for a settings leaf."""
    return Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {
                    "CNAME": cname,
                    "TYPE": "T_ACTION_V1",
                    "NAME": cname,
                    "VALUE": value,
                    "HASHVAL": hashval,
                }
            ],
        }
    )


async def test_start_ap_scan_fires_an_action_with_the_fetched_hashval() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("start_ap_search", 300381621, "T_ACTION_V1"),
        Response.from_json({"STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"}}),
    ]
    await _soundbar(client).start_ap_scan()

    get_call, put_call = client.request_spec.call_args_list
    assert get_call.args[0].method == "GET"
    assert put_call.args[0].method == "PUT"
    assert put_call.kwargs["body"] == {"REQUEST": "ACTION", "HASHVAL": 300381621}


async def test_stop_ap_scan_fires_an_action() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("stop_ap_search", 139197155, "T_ACTION_V1"),
        Response.from_json({"STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"}}),
    ]
    await _soundbar(client).stop_ap_scan()
    assert client.request_spec.call_args_list[1].kwargs["body"] == {
        "REQUEST": "ACTION",
        "HASHVAL": 139197155,
    }


async def test_get_access_points_returns_parsed_networks() -> None:
    client = AsyncMock()
    client.request_spec.return_value = _aps_response()
    aps = await _soundbar(client).get_access_points()
    assert [a.ssid for a in aps] == ["net-5g", "net-24"]


async def test_get_current_access_point_returns_none_when_unset() -> None:
    client = AsyncMock()
    client.request_spec.return_value = Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {
                    "CNAME": "current_access_point",
                    "TYPE": "T_AP_V1",
                    "NAME": "Current Access Point",
                    "VALUE": [
                        {
                            "EM": "NONE",
                            "RSSI": 0,
                            "NAME": "",
                            "BSSID": "000000-000000",
                            "BAND": "2.4",
                        }
                    ],
                }
            ],
        }
    )
    assert await _soundbar(client).get_current_access_point() is None


async def test_missing_hashval_raises_response_error() -> None:
    client = AsyncMock()
    client.request_spec.return_value = Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {
                    "CNAME": "start_ap_search",
                    "TYPE": "T_ACTION_V1",
                    "NAME": "Start AP Search",
                    "VALUE": "T_ACTION_V1",
                }
            ],
        }
    )
    with pytest.raises(VizioResponseError, match="HASHVAL"):
        await _soundbar(client).start_ap_scan()


def _ok() -> Response:
    """A bare SUCCESS envelope, as returned by a settings PUT."""
    return Response.from_json({"STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"}})


async def test_join_access_point_selects_then_sets_password() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("current_access_point", 3250072061),
        _ok(),
        _leaf_response("set_wifi_password", 2698362233),
        _ok(),
    ]
    await _soundbar(client).join_access_point("MinasTirith", password="s3cret")

    calls = client.request_spec.call_args_list
    assert calls[1].kwargs["body"] == {
        "REQUEST": "MODIFY",
        "VALUE": [{"NAME": "MinasTirith"}],
        "HASHVAL": 3250072061,
    }
    assert calls[3].kwargs["body"] == {
        "REQUEST": "MODIFY",
        "VALUE": "s3cret",
        "HASHVAL": 2698362233,
    }
    # Ordering matters: selection must land before the password.
    assert calls[1].args[0].paths[0].endswith("/current_access_point")
    assert calls[3].args[0].paths[0].endswith("/set_wifi_password")


async def test_join_access_point_sends_empty_password_when_omitted() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("current_access_point", 1),
        _ok(),
        _leaf_response("set_wifi_password", 2),
        _ok(),
    ]
    await _soundbar(client).join_access_point("open-net")
    assert client.request_spec.call_args_list[3].kwargs["body"]["VALUE"] == ""


async def test_join_access_point_hidden_uses_one_put() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [_leaf_response("hidden_network", 99), _ok()]
    await _soundbar(client).join_access_point("ghost", password="pw", hidden=True)

    assert client.request_spec.call_count == 2
    assert client.request_spec.call_args_list[1].kwargs["body"] == {
        "REQUEST": "MODIFY",
        "VALUE": [{"NAME": "ghost", "PASSWORD": "pw"}],
        "HASHVAL": 99,
    }


async def test_join_access_point_tolerates_already_connected() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("current_access_point", 1),
        VizioWifiError(WifiResult.ALREADY_CONNECTED, "NET_WIFI_ALREADY_CONNECTED"),
    ]
    # Must not raise: the device is already where we wanted it.
    await _soundbar(client).join_access_point("MinasTirith", password="pw")


async def test_join_access_point_propagates_auth_rejection() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("current_access_point", 1),
        _ok(),
        _leaf_response("set_wifi_password", 2),
        VizioWifiError(WifiResult.AUTH_REJECTED, "NET_WIFI_AUTH_REJECTED"),
    ]
    with pytest.raises(VizioWifiError) as excinfo:
        await _soundbar(client).join_access_point("MinasTirith", password="wrong")
    assert excinfo.value.result is WifiResult.AUTH_REJECTED


class _FakeVizio:
    """Records provisioning calls; lets tests inject failures."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.join_error: Exception | None = None
        self.aps: tuple[AccessPoint, ...] = ()

    async def start_ap_scan(self) -> None:
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error

    async def stop_ap_scan(self) -> None:
        self.calls.append("stop")
        if self.stop_error is not None:
            raise self.stop_error

    async def get_access_points(self) -> tuple[AccessPoint, ...]:
        self.calls.append("read")
        return self.aps

    async def join_access_point(
        self, ssid: str, *, password: str | None = None, hidden: bool = False
    ) -> None:
        self.calls.append(f"join:{ssid}:{password}:{hidden}")
        if self.join_error is not None:
            raise self.join_error


async def test_session_starts_and_stops_the_scan() -> None:
    fake = _FakeVizio()
    async with WifiSetupSession(fake):  # type: ignore[arg-type]
        pass
    assert fake.calls == ["start", "stop"]


async def test_session_stops_the_scan_when_the_body_raises() -> None:
    fake = _FakeVizio()
    with pytest.raises(RuntimeError, match="boom"):
        async with WifiSetupSession(fake):  # type: ignore[arg-type]
            raise RuntimeError("boom")
    assert fake.calls == ["start", "stop"]


async def test_session_stops_the_scan_when_join_raises() -> None:
    fake = _FakeVizio()
    fake.join_error = VizioWifiError(WifiResult.AUTH_REJECTED, "NET_WIFI_AUTH_REJECTED")
    with pytest.raises(VizioWifiError):
        async with WifiSetupSession(fake) as session:  # type: ignore[arg-type]
            await session.join("net", password="wrong")
    assert fake.calls[-1] == "stop"


async def test_failed_stop_does_not_mask_the_callers_exception() -> None:
    fake = _FakeVizio()
    fake.stop_error = VizioWifiError(WifiResult.UNKNOWN, "NET_UNKNOWN_ERROR")
    with pytest.raises(RuntimeError, match="original"):
        async with WifiSetupSession(fake):  # type: ignore[arg-type]
            raise RuntimeError("original")


async def test_failed_start_still_attempts_a_stop() -> None:
    fake = _FakeVizio()
    fake.start_error = VizioResponseError("nope")
    with pytest.raises(VizioResponseError):
        async with WifiSetupSession(fake):  # type: ignore[arg-type]
            pass
    assert fake.calls == ["start", "stop"]


async def test_access_points_is_re_readable() -> None:
    fake = _FakeVizio()
    async with WifiSetupSession(fake) as session:  # type: ignore[arg-type]
        await session.access_points()
        await session.access_points()
    assert fake.calls.count("read") == 2


async def test_join_is_re_callable_after_a_failure() -> None:
    fake = _FakeVizio()
    async with WifiSetupSession(fake) as session:  # type: ignore[arg-type]
        await session.join("net", password="first")
        await session.join("net", password="second")
    assert "join:net:first:False" in fake.calls
    assert "join:net:second:False" in fake.calls


async def test_wifi_setup_session_factory_returns_a_session() -> None:
    device = Vizio(host="h:9000", device_type=DeviceType.SOUNDBAR)
    assert isinstance(device.wifi_setup_session(), WifiSetupSession)
    await device.aclose()
