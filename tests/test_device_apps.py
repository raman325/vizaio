"""
Integration tests for the Vizio class's app + availability path.

Covers:
- Cache injection via constructor (HA-coordinator pattern)
- ``list_available_apps`` with chipset/firmware overrides
- ``list_apps`` returns the full catalog regardless of availability
- ``launch_app`` resolves a payload via availability when the catalog
  has no legacy ``config[]``
- ``parse_vizios_binary`` / ``parse_firmware_version`` against a
  realistic deviceinfo payload
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest

from vizaio import (
    AppAvailability,
    AppConfig,
    AppRecord,
    ChipsetPayload,
    DeviceType,
    Vizio,
)
from vizaio.parse import parse_firmware_version, parse_vizios_binary
from vizaio.wire import Response


def _av(app_id: str, **chipsets: list[ChipsetPayload]) -> AppAvailability:
    return AppAvailability(
        app_id=app_id,
        chipsets=MappingProxyType({k: tuple(v) for k, v in chipsets.items()}),
    )


# ---------------------------------------------------------------------------
# parse_vizios_binary / parse_firmware_version
# ---------------------------------------------------------------------------


class TestParseDeviceInfoFields:
    def test_vizios_binary(self, deviceinfo_response: Response) -> None:
        # The wire layer lowercases keys, so ``BINARIES.ViziOS`` becomes
        # ``binaries.vizios``. Live capture from VHD24M-0810.
        assert parse_vizios_binary(deviceinfo_response) == "mtk5583-1.6.1312.1"

    def test_firmware_version(self, deviceinfo_response: Response) -> None:
        assert parse_firmware_version(deviceinfo_response) == "3.720.9.1-1"

    def test_missing_binaries_returns_empty(self) -> None:
        empty_response = Response.from_json(
            {
                "ITEMS": [{"VALUE": {}, "CNAME": "deviceinfo"}],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        assert parse_vizios_binary(empty_response) == ""

    def test_missing_system_info_returns_empty(self) -> None:
        empty_response = Response.from_json(
            {
                "ITEMS": [{"VALUE": {}, "CNAME": "deviceinfo"}],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        assert parse_firmware_version(empty_response) == ""


# ---------------------------------------------------------------------------
# Cache injection (HA-coordinator pattern)
# ---------------------------------------------------------------------------


class TestCacheInjection:
    """Caller passes pre-fetched apps + availability into the Vizio
    constructor; the instance never auto-fetches them."""

    async def test_injected_cache_used_without_remote_call(self) -> None:
        record = AppRecord(name="Injected", country=("*",), id="1")
        avail = _av("1", **{"*": [ChipsetPayload(AppConfig("1", 2))]})

        with (
            patch(
                "vizaio._device.fetch_app_catalog",
                new=AsyncMock(side_effect=AssertionError("should not fetch")),
            ),
            patch(
                "vizaio._device.fetch_app_availability",
                new=AsyncMock(side_effect=AssertionError("should not fetch")),
            ),
        ):
            vizio = Vizio(
                "1.2.3.4",
                device_type=DeviceType.TV,
                apps=(record,),
                availability=(avail,),
            )
            try:
                catalog = await vizio._get_app_catalog()
                availability = await vizio._get_app_availability()
            finally:
                await vizio.aclose()

        assert catalog == (record,)
        assert availability == (avail,)


class TestCacheInjectionViaSetters:
    """Caller mutates cached catalog or availability on an existing Vizio
    instance; subsequent reads use the new value and the lib doesn't
    auto-fetch (HA apps-coordinator pattern, post-construction)."""

    async def test_set_app_catalog_installs_value_without_fetch(self) -> None:
        record = AppRecord(name="Pushed", country=("*",), id="42")

        with patch(
            "vizaio._device.fetch_app_catalog",
            new=AsyncMock(side_effect=AssertionError("should not fetch")),
        ):
            vizio = Vizio("1.2.3.4", device_type=DeviceType.TV)
            try:
                vizio.set_app_catalog((record,))
                catalog = await vizio._get_app_catalog()
            finally:
                await vizio.aclose()

        assert catalog == (record,)

    async def test_set_app_availability_installs_value_without_fetch(self) -> None:
        avail = _av("1", **{"*": [ChipsetPayload(AppConfig("1", 2))]})

        with patch(
            "vizaio._device.fetch_app_availability",
            new=AsyncMock(side_effect=AssertionError("should not fetch")),
        ):
            vizio = Vizio("1.2.3.4", device_type=DeviceType.TV)
            try:
                vizio.set_app_availability((avail,))
                availability = await vizio._get_app_availability()
            finally:
                await vizio.aclose()

        assert availability == (avail,)

    async def test_setter_overrides_constructor_injection(self) -> None:
        old = AppRecord(name="Old", country=("*",), id="1")
        new = AppRecord(name="New", country=("*",), id="2")

        with patch(
            "vizaio._device.fetch_app_catalog",
            new=AsyncMock(side_effect=AssertionError("should not fetch")),
        ):
            vizio = Vizio("1.2.3.4", device_type=DeviceType.TV, apps=(old,))
            try:
                vizio.set_app_catalog((new,))
                catalog = await vizio._get_app_catalog()
            finally:
                await vizio.aclose()

        assert catalog == (new,)

    async def test_set_app_catalog_accepts_empty_tuple(self) -> None:
        with patch(
            "vizaio._device.fetch_app_catalog",
            new=AsyncMock(side_effect=AssertionError("should not fetch")),
        ):
            vizio = Vizio("1.2.3.4", device_type=DeviceType.TV)
            try:
                vizio.set_app_catalog(())
                catalog = await vizio._get_app_catalog()
            finally:
                await vizio.aclose()

        assert catalog == ()


# ---------------------------------------------------------------------------
# list_available_apps + list_apps
# ---------------------------------------------------------------------------


class TestListAvailableApps:
    async def test_filters_to_available_apps(self) -> None:
        avail_only = AppRecord(name="Avail", country=("*",), id="1")
        no_avail = AppRecord(name="NoAvail", country=("*",), id="2")
        avail = _av("1", **{"*": [ChipsetPayload(AppConfig("1", 2))]})

        async with Vizio(
            "1.2.3.4",
            device_type=DeviceType.TV,
            apps=(avail_only, no_avail),
            availability=(avail,),
        ) as v:
            # Stub chipset/firmware probes — would normally hit the
            # device on first call.
            v._cached_chipset = ""
            v._cached_firmware = ""
            apps = await v.list_available_apps()

        names = {r.name for r in apps}
        assert names == {"Avail"}

    async def test_chipset_override(self) -> None:
        """Override the auto-detected chipset to preview a different SoC."""
        record = AppRecord(name="MT5586App", country=("*",), id="1")
        # Only MT5586 has a payload — wildcard is empty.
        avail = _av("1", MT5586=[ChipsetPayload(AppConfig("1", 2))])

        async with Vizio(
            "1.2.3.4",
            device_type=DeviceType.TV,
            apps=(record,),
            availability=(avail,),
        ) as v:
            # Auto-detected chipset would be MT5583 (no match), but
            # override forces MT5586 (match).
            v._cached_chipset = "MT5583"
            v._cached_firmware = ""
            assert (await v.list_available_apps()) == []
            assert (await v.list_available_apps(chipset="MT5586")) == [record]

    async def test_firmware_override(self) -> None:
        record = AppRecord(name="NewFW", country=("*",), id="1")
        # Payload requires firmware ≥ 5.0.0.
        avail = _av(
            "1",
            **{"*": [ChipsetPayload(AppConfig("1", 2), firmware_minimum="5.0.0.0-1")]},
        )

        async with Vizio(
            "1.2.3.4",
            device_type=DeviceType.TV,
            apps=(record,),
            availability=(avail,),
        ) as v:
            v._cached_chipset = ""
            v._cached_firmware = "3.0.0.0-0"
            # Auto-detected fw is below the minimum — filtered out.
            assert (await v.list_available_apps()) == []
            # Override to a fw above the minimum — included.
            assert (await v.list_available_apps(firmware="5.1.0.0-0")) == [record]

    async def test_country_filter(self) -> None:
        usa = AppRecord(name="USAOnly", country=("USA",), id="1")
        worldwide = AppRecord(name="World", country=("*",), id="2")
        avail = (
            _av("1", **{"*": [ChipsetPayload(AppConfig("a", 2))]}),
            _av("2", **{"*": [ChipsetPayload(AppConfig("b", 2))]}),
        )

        async with Vizio(
            "1.2.3.4",
            device_type=DeviceType.TV,
            apps=(usa, worldwide),
            availability=avail,
        ) as v:
            v._cached_chipset = ""
            v._cached_firmware = ""
            usa_only = await v.list_available_apps(country="USA")
            mex_only = await v.list_available_apps(country="MEX")
            unfiltered = await v.list_available_apps()

        assert {r.name for r in usa_only} == {"USAOnly", "World"}
        # MEX falls through to wildcard ("*") match for World only.
        assert {r.name for r in mex_only} == {"World"}
        assert {r.name for r in unfiltered} == {"USAOnly", "World"}


class TestListApps:
    async def test_returns_full_catalog_unfiltered(self) -> None:
        # No availability data at all — list_available_apps would return
        # nothing, but list_apps returns everything.
        a = AppRecord(name="A", country=("*",), id="1")
        b = AppRecord(name="B", country=("*",), id="2")

        async with Vizio(
            "1.2.3.4",
            device_type=DeviceType.TV,
            apps=(a, b),
            availability=(),
        ) as v:
            v._cached_chipset = ""
            v._cached_firmware = ""
            assert await v.list_apps() == [a, b]
            assert await v.list_available_apps() == []


class TestListAvailableLegacyConfigBackwardCompat:
    async def test_record_with_legacy_config_included_without_availability(
        self,
    ) -> None:
        """Archived catalog dumps that ship launch payloads inline don't
        depend on availability data being joinable. Such records should
        still appear in list_available_apps without a matching
        AppAvailability entry."""
        legacy = AppRecord(
            name="LegacyApp",
            country=("*",),
            config=(AppConfig(app_id="leg", name_space=2),),
            id="999",
        )
        modern_no_avail = AppRecord(name="Modern", country=("*",), id="1000")

        async with Vizio(
            "1.2.3.4",
            device_type=DeviceType.TV,
            apps=(legacy, modern_no_avail),
            availability=(),
        ) as v:
            v._cached_chipset = ""
            v._cached_firmware = ""
            apps = await v.list_available_apps()

        # Legacy entry kept (launchable from inline config[0]); modern
        # entry without availability dropped.
        assert [r.name for r in apps] == ["LegacyApp"]


# ---------------------------------------------------------------------------
# launch_app via availability
# ---------------------------------------------------------------------------


class TestLaunchAppViaAvailability:
    async def test_resolves_payload_from_availability(self) -> None:
        """When AppRecord has no legacy config, look up via availability
        and call launch_app_config with the resolved payload."""
        record = AppRecord(name="ModernApp", country=("*",), id="44")
        avail = _av("44", MT5583=[ChipsetPayload(AppConfig("1", 5))])
        captured: list[AppConfig] = []

        async with Vizio(
            "1.2.3.4",
            device_type=DeviceType.TV,
            apps=(record,),
            availability=(avail,),
        ) as v:
            v._cached_chipset = "MT5583"
            v._cached_firmware = "3.720.9.1-1"
            v.launch_app_config = AsyncMock(side_effect=captured.append)
            await v.launch_app("ModernApp")

        assert captured == [AppConfig("1", 5)]

    async def test_chipset_override_changes_launch_payload(self) -> None:
        record = AppRecord(name="App", country=("*",), id="1")
        # Different payload per chipset.
        avail = _av(
            "1",
            MT5583=[ChipsetPayload(AppConfig("a", 2))],
            MT5586=[ChipsetPayload(AppConfig("b", 2))],
        )
        captured: list[AppConfig] = []

        async with Vizio(
            "1.2.3.4",
            device_type=DeviceType.TV,
            apps=(record,),
            availability=(avail,),
        ) as v:
            v._cached_chipset = "MT5583"
            v._cached_firmware = ""
            v.launch_app_config = AsyncMock(side_effect=captured.append)
            await v.launch_app("App", chipset="MT5586")

        # Override won — payload comes from MT5586 entry.
        assert captured == [AppConfig("b", 2)]

    async def test_legacy_config_short_circuits(self) -> None:
        """When AppRecord has a legacy config[0], launch uses it directly
        without consulting availability."""
        record = AppRecord(
            name="LegacyApp",
            country=("*",),
            config=(AppConfig(app_id="leg", name_space=2),),
            id="1",
        )
        avail = _av("1", **{"*": [ChipsetPayload(AppConfig("avail", 9))]})
        captured: list[AppConfig] = []

        async with Vizio(
            "1.2.3.4",
            device_type=DeviceType.TV,
            apps=(record,),
            availability=(avail,),
        ) as v:
            v.launch_app_config = AsyncMock(side_effect=captured.append)
            await v.launch_app("LegacyApp")

        # Legacy config[0] won — availability not used.
        assert captured == [AppConfig("leg", 2)]

    async def test_launch_unknown_app_raises(self) -> None:
        async with Vizio(
            "1.2.3.4",
            device_type=DeviceType.TV,
            apps=(),
            availability=(),
        ) as v:
            from vizaio import VizioInvalidParameterError

            with pytest.raises(VizioInvalidParameterError, match="not in catalog"):
                await v.launch_app("Nope")
