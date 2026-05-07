"""Network discovery: zeroconf + SSDP, run concurrently and merged.

The unified ``discover()`` runs both protocols in parallel and dedupes by
IP. Most TVs surface via mDNS (``_viziocast._tcp.local.``); soundbars and
Crave 360s typically come via SSDP (``urn:dial-multiscreen-org:device:dial:1``
filtered to manufacturer == "VIZIO").

Tests:
1. ``discover_zeroconf`` returns DiscoveredDevice records for fake mDNS
   service answers (zeroconf library is mocked end-to-end).
2. ``discover_ssdp`` parses M-SEARCH responses + DIAL XML descriptions,
   filters non-Vizio manufacturers.
3. ``discover()`` runs both concurrently, dedupes, prefers zeroconf
   metadata when both find the same IP.
4. ``discover_zeroconf`` raises ``ImportError`` with install hint when
   the ``[discovery]`` extra is not installed.

Implementation lands in #27. Tests fail until then.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from vizaio import (
    DeviceType,
    DiscoveredDevice,
    Vizio,
    VizioAuthError,
    VizioConnectionError,
)
from vizaio.discovery import (
    async_classify_device,
    async_is_tv,
    classify_crave_model,
    discover,
    discover_ssdp,
    discover_zeroconf,
    is_crave_model,
)
from vizaio.wire import Response

# ---------------------------------------------------------------------------
# Zeroconf
# ---------------------------------------------------------------------------


class TestDiscoverZeroconf:
    """Unit tests for the zeroconf path. The zeroconf library itself is
    mocked — we trust it produces correct mDNS but we verify our
    DiscoveredDevice mapping."""

    async def test_finds_tv(self) -> None:
        # Patch the AsyncZeroconf-based scan to return one fake service.
        fake_service = _make_fake_zeroconf_info(
            name="Living Room TV",
            address="192.168.1.50",
            port=7345,
            properties={b"name": b"V505-G9", b"id": b"abc123"},
        )
        with patch(
            "vizaio.discovery._zeroconf_scan",
            return_value=[fake_service],
        ):
            results = await discover_zeroconf(timeout=0.1)
        assert results == [
            DiscoveredDevice(
                name="Living Room TV",
                ip="192.168.1.50",
                port=7345,
                model="V505-G9",
                id="abc123",
            )
        ]

    async def test_handles_missing_id(self) -> None:
        fake_service = _make_fake_zeroconf_info(
            name="TV",
            address="192.168.1.51",
            port=7345,
            properties={b"name": b"V435-J01"},  # no 'id' property
        )
        with patch(
            "vizaio.discovery._zeroconf_scan",
            return_value=[fake_service],
        ):
            results = await discover_zeroconf(timeout=0.1)
        assert results[0].id == ""

    async def test_returns_empty_when_no_services(self) -> None:
        with patch(
            "vizaio.discovery._zeroconf_scan",
            return_value=[],
        ):
            results = await discover_zeroconf(timeout=0.1)
        assert results == []

    async def test_extra_not_installed_raises_helpful_error(self) -> None:
        """When zeroconf isn't installed, raise ImportError with the
        install command, not a cryptic ModuleNotFoundError."""
        with (
            patch(
                "vizaio.discovery._ZEROCONF_AVAILABLE",
                False,
            ),
            pytest.raises(ImportError, match=r"vizaio\[discovery\]"),
        ):
            await discover_zeroconf()


# ---------------------------------------------------------------------------
# SSDP
# ---------------------------------------------------------------------------


class TestDiscoverSsdp:
    """SSDP returns M-SEARCH responses; for each we fetch the XML
    description and parse it. Non-Vizio devices are filtered out."""

    async def test_finds_soundbar(self) -> None:
        ssdp_response = _make_fake_ssdp_response(
            location="http://192.168.1.51:8008/ssdp/device-desc.xml"
        )
        xml_description = _make_dial_xml(
            manufacturer="VIZIO",
            friendly_name="Vizio SB3651",
            model="SB3651-E6",
            udn="uuid:abc-123",
        )
        with (
            patch(
                "vizaio.discovery._ssdp_msearch",
                return_value=[ssdp_response],
            ),
            patch(
                "vizaio.discovery._fetch_dial_xml",
                return_value=xml_description,
            ),
        ):
            results = await discover_ssdp(timeout=0.1)
        assert len(results) == 1
        assert results[0].name == "Vizio SB3651"
        assert results[0].model == "SB3651-E6"
        assert results[0].ip == "192.168.1.51"
        assert results[0].port == 8008

    async def test_filters_non_vizio_manufacturers(self) -> None:
        """SSDP DIAL is generic — Chromecasts, Roku, Fire TV all respond.
        We must filter to manufacturer=='VIZIO' only."""
        ssdp_response = _make_fake_ssdp_response(
            location="http://192.168.1.99:8008/ssdp/device-desc.xml"
        )
        chromecast_xml = _make_dial_xml(
            manufacturer="Google Inc.",
            friendly_name="Living Room Chromecast",
            model="Chromecast",
            udn="uuid:goog-1",
        )
        with (
            patch(
                "vizaio.discovery._ssdp_msearch",
                return_value=[ssdp_response],
            ),
            patch(
                "vizaio.discovery._fetch_dial_xml",
                return_value=chromecast_xml,
            ),
        ):
            results = await discover_ssdp(timeout=0.1)
        assert results == []

    async def test_skips_unfetchable_xml(self) -> None:
        """If the device doesn't serve its XML description (firewalled,
        slow, broken), skip it rather than abort the whole scan."""
        ssdp_response = _make_fake_ssdp_response(
            location="http://192.168.1.99:8008/ssdp/device-desc.xml"
        )
        with (
            patch(
                "vizaio.discovery._ssdp_msearch",
                return_value=[ssdp_response],
            ),
            patch(
                "vizaio.discovery._fetch_dial_xml",
                side_effect=TimeoutError(),
            ),
        ):
            results = await discover_ssdp(timeout=0.1)
        assert results == []


# ---------------------------------------------------------------------------
# Unified discover()
# ---------------------------------------------------------------------------


class TestDiscoverUnified:
    """``discover()`` runs zeroconf + SSDP concurrently, merges by IP."""

    async def test_runs_concurrently(self) -> None:
        """Verify both protocols are awaited in parallel, not sequentially.
        We can't assert wall-clock time without flakiness, but we can
        assert both helpers were called."""
        with (
            patch(
                "vizaio.discovery.discover_zeroconf",
                return_value=[],
            ) as zc,
            patch(
                "vizaio.discovery.discover_ssdp",
                return_value=[],
            ) as ss,
        ):
            await discover(timeout=0.1)
        zc.assert_awaited_once()
        ss.assert_awaited_once()

    async def test_dedupes_by_ip(self) -> None:
        """Same TV via both protocols — return one entry, prefer zeroconf
        metadata (it's typically richer)."""
        zc_result = [
            DiscoveredDevice(
                name="TV (zeroconf)",
                ip="192.168.1.50",
                port=7345,
                model="V505-G9",
                id="abc123",
            )
        ]
        ssdp_result = [
            DiscoveredDevice(
                name="TV (ssdp)",
                ip="192.168.1.50",
                port=8008,
                model="V505-G9",
                id="uuid:xyz",
            )
        ]
        with (
            patch(
                "vizaio.discovery.discover_zeroconf",
                return_value=zc_result,
            ),
            patch(
                "vizaio.discovery.discover_ssdp",
                return_value=ssdp_result,
            ),
        ):
            results = await discover(timeout=0.1)
        assert len(results) == 1
        # Zeroconf metadata wins.
        assert results[0].name == "TV (zeroconf)"

    async def test_merges_distinct_devices(self) -> None:
        """A TV via zeroconf and a soundbar via SSDP — both surfaced."""
        with (
            patch(
                "vizaio.discovery.discover_zeroconf",
                return_value=[
                    DiscoveredDevice(
                        name="TV", ip="192.168.1.50", port=7345, model="V505"
                    )
                ],
            ),
            patch(
                "vizaio.discovery.discover_ssdp",
                return_value=[
                    DiscoveredDevice(
                        name="SB", ip="192.168.1.51", port=8008, model="SB3651"
                    )
                ],
            ),
        ):
            results = await discover(timeout=0.1)
        assert len(results) == 2
        ips = {d.ip for d in results}
        assert ips == {"192.168.1.50", "192.168.1.51"}

    async def test_continues_when_zeroconf_extra_missing(self) -> None:
        """If the user hasn't installed [discovery], discover() should
        still try SSDP and return its results rather than aborting."""
        with (
            patch(
                "vizaio.discovery.discover_zeroconf",
                side_effect=ImportError("install zeroconf"),
            ),
            patch(
                "vizaio.discovery.discover_ssdp",
                return_value=[
                    DiscoveredDevice(
                        name="SB", ip="192.168.1.51", port=8008, model="SB"
                    )
                ],
            ),
        ):
            results = await discover(timeout=0.1)
        assert len(results) == 1
        assert results[0].ip == "192.168.1.51"


# ---------------------------------------------------------------------------
# DiscoveredDevice convenience
# ---------------------------------------------------------------------------


class TestDiscoveredDeviceHost:
    """The .host property combines ip:port — used as the Vizio constructor
    arg directly."""

    def test_host(self) -> None:
        d = DiscoveredDevice(name="x", ip="192.168.1.50", port=7345, model="y")
        assert d.host == "192.168.1.50:7345"


# ---------------------------------------------------------------------------
# async_is_tv — pre-construction probe used by HA's config flow
# ---------------------------------------------------------------------------


class TestAsyncIsTv:
    """Probe a host to determine whether it's a TV (auth-gated settings
    tree) or an audio device (open settings tree). Mirrors pyvizio's
    ``async_guess_device_type`` lenient semantics: any failure
    classifies as TV (the dominant case for HA users)."""

    async def test_returns_false_when_settings_succeed_unauthenticated(
        self,
    ) -> None:
        with patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)):
            assert await async_is_tv("1.2.3.4:9000") is False

    async def test_returns_true_when_auth_required(self) -> None:
        with patch.object(
            Vizio,
            "ping_auth",
            new=AsyncMock(side_effect=VizioAuthError("REQUIRES_PAIRING")),
        ):
            assert await async_is_tv("1.2.3.4:7345") is True

    async def test_returns_true_on_connection_error(self) -> None:
        # Lenient like pyvizio: an unreachable device defaults to TV
        # rather than raising. HA's config_flow only needs a binary
        # answer; a wrong guess gets corrected when the user is shown
        # the form.
        with patch.object(
            Vizio,
            "ping_auth",
            new=AsyncMock(side_effect=VizioConnectionError("unreachable")),
        ):
            assert await async_is_tv("1.2.3.4:7345") is True

    async def test_port_kwarg_appended_to_host(self) -> None:
        captured: list[str] = []

        async def capture_host(self: Vizio) -> None:
            captured.append(self.host)

        with patch.object(Vizio, "ping_auth", new=capture_host):
            await async_is_tv("1.2.3.4", port=7345)

        assert captured == ["1.2.3.4:7345"]

    async def test_port_kwarg_conflicts_with_inline_port(self) -> None:
        with pytest.raises(ValueError, match=r"port"):
            await async_is_tv("1.2.3.4:7345", port=7345)


class TestIsCraveModel:
    """Pure prefix check for Vizio's Crave product line. Crave models
    follow the ``SP*-*`` pattern (``SP30-E0``, ``SP50-D5``,
    ``SP70-D5``); no other Vizio audio device uses this prefix."""

    def test_sp30_is_crave(self) -> None:
        assert is_crave_model("SP30-E0") is True

    def test_sp50_is_crave(self) -> None:
        assert is_crave_model("SP50-D5") is True

    def test_sp70_is_crave(self) -> None:
        assert is_crave_model("SP70-D5") is True

    def test_lowercase_is_crave(self) -> None:
        assert is_crave_model("sp30-e0") is True

    def test_tv_model_is_not_crave(self) -> None:
        assert is_crave_model("V505-G9") is False

    def test_soundbar_model_is_not_crave(self) -> None:
        # Vizio soundbars use prefixes like SB36*, S5*, M5*.
        assert is_crave_model("SB36514-G6") is False

    def test_empty_string_is_not_crave(self) -> None:
        assert is_crave_model("") is False


class TestClassifyCraveModel:
    """Maps a Crave-family model string to its specific
    ``DeviceType`` variant. Precondition: caller must have verified
    ``is_crave_model(model)`` first."""

    def test_sp30_maps_to_crave_go(self) -> None:
        assert classify_crave_model("SP30-E0") is DeviceType.CRAVE_GO

    def test_sp50_maps_to_crave360(self) -> None:
        assert classify_crave_model("SP50-D5") is DeviceType.CRAVE360

    def test_sp70_maps_to_crave_pro(self) -> None:
        assert classify_crave_model("SP70-D5") is DeviceType.CRAVE_PRO

    def test_lowercase_resolves_correctly(self) -> None:
        assert classify_crave_model("sp50-d5") is DeviceType.CRAVE360

    def test_unknown_sp_variant_falls_back_to_crave_go(self) -> None:
        # Lenient default: unknown SP* models default to the
        # lowest-spec variant. Forecasted-wrong max_volume is safer
        # when guessed low than guessed high.
        assert classify_crave_model("SP99-X1") is DeviceType.CRAVE_GO

    def test_non_crave_model_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"not a Crave"):
            classify_crave_model("V505-G9")

    def test_empty_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"not a Crave"):
            classify_crave_model("")


class TestAsyncClassifyDevice:
    """Composer that walks the three classification layers. Returns
    ``DeviceType.TV`` when the auth probe says TV. Otherwise fetches
    deviceinfo unauthenticated and reads ``SYSTEM_INFO.MODEL_NAME``
    to refine into SOUNDBAR vs CRAVE_*."""

    async def test_tv_short_circuits_at_layer_1(self) -> None:
        # async_is_tv → True means we don't hit deviceinfo at all.
        with patch.object(
            Vizio,
            "ping_auth",
            new=AsyncMock(side_effect=VizioAuthError("REQUIRES_PAIRING")),
        ):
            result = await async_classify_device("1.2.3.4:7345")
        assert result is DeviceType.TV

    async def test_soundbar_path(self) -> None:
        deviceinfo = Response.from_json(
            {
                "ITEMS": [
                    {
                        "VALUE": {"SYSTEM_INFO": {"MODEL_NAME": "SB36514-G6"}},
                        "CNAME": "deviceinfo",
                    }
                ],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(Vizio, "_request", new=AsyncMock(return_value=deviceinfo)),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.SOUNDBAR

    async def test_crave_go_path(self) -> None:
        deviceinfo = Response.from_json(
            {
                "ITEMS": [
                    {
                        "VALUE": {"SYSTEM_INFO": {"MODEL_NAME": "SP30-E0"}},
                        "CNAME": "deviceinfo",
                    }
                ],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(Vizio, "_request", new=AsyncMock(return_value=deviceinfo)),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.CRAVE_GO

    async def test_crave_360_path(self) -> None:
        deviceinfo = Response.from_json(
            {
                "ITEMS": [
                    {
                        "VALUE": {"SYSTEM_INFO": {"MODEL_NAME": "SP50-D5"}},
                        "CNAME": "deviceinfo",
                    }
                ],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(Vizio, "_request", new=AsyncMock(return_value=deviceinfo)),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.CRAVE360

    async def test_crave_pro_path(self) -> None:
        deviceinfo = Response.from_json(
            {
                "ITEMS": [
                    {
                        "VALUE": {"SYSTEM_INFO": {"MODEL_NAME": "SP70-D5"}},
                        "CNAME": "deviceinfo",
                    }
                ],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(Vizio, "_request", new=AsyncMock(return_value=deviceinfo)),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.CRAVE_PRO

    async def test_deviceinfo_fetch_failure_falls_back_to_soundbar(
        self,
    ) -> None:
        # async_is_tv → False, but the deviceinfo fetch raises.
        # Lenient default: SOUNDBAR (we know it's not a TV from layer 1).
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(
                Vizio,
                "_request",
                new=AsyncMock(side_effect=VizioConnectionError("dropped")),
            ),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.SOUNDBAR

    async def test_deviceinfo_with_empty_model_falls_back_to_soundbar(
        self,
    ) -> None:
        # async_is_tv → False, deviceinfo returns but SYSTEM_INFO is missing.
        deviceinfo = Response.from_json(
            {
                "ITEMS": [{"VALUE": {}, "CNAME": "deviceinfo"}],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(Vizio, "_request", new=AsyncMock(return_value=deviceinfo)),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.SOUNDBAR


# ---------------------------------------------------------------------------
# Helpers — fake protocol-level responses for isolation tests
# ---------------------------------------------------------------------------


def _make_fake_zeroconf_info(
    *,
    name: str,
    address: str,
    port: int,
    properties: dict[bytes, bytes],
):
    """Construct a minimal stand-in for ``zeroconf.AsyncServiceInfo``.

    Implementation will mirror what ``_zeroconf_scan`` returns from the
    real library. Tests here mock the scan, so this is just a duck-typed
    object the code can call ``.parsed_addresses()``, ``.port``, ``.name``,
    ``.properties`` on.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        name=f"{name}._viziocast._tcp.local.",
        type="_viziocast._tcp.local.",
        port=port,
        properties=properties,
        parsed_addresses=lambda *args: [address],
    )


def _make_fake_ssdp_response(*, location: str):
    """Stand-in for an SSDP M-SEARCH response."""
    from types import SimpleNamespace

    return SimpleNamespace(location=location)


def _make_dial_xml(
    *,
    manufacturer: str,
    friendly_name: str,
    model: str,
    udn: str,
) -> bytes:
    """Build a minimal valid DIAL device-description XML."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:dial-multiscreen-org:device:dial:1</deviceType>
    <friendlyName>{friendly_name}</friendlyName>
    <manufacturer>{manufacturer}</manufacturer>
    <modelName>{model}</modelName>
    <UDN>{udn}</UDN>
  </device>
</root>
""".encode()
