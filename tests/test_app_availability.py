"""
Tests for the chipset/firmware availability layer.

Covers:
- availability JSON parsing (schema, malformed entries, null ids)
- chipset extraction from ``BINARIES.ViziOS`` strings
- firmware-version comparison (tuple-of-ints with permissive fallback)
- ``pick_chipset_payload`` resolution order (specific → wildcard)
- catalog ↔ availability join (``find_app_name`` reverse lookup)
"""

from __future__ import annotations

from types import MappingProxyType

import aiohttp
from aioresponses import aioresponses
import pytest

from vizaio import (
    AppAvailability,
    AppConfig,
    AppRecord,
    ChipsetPayload,
)
from vizaio.apps import (
    BUNDLED_AVAILABILITY,
    REMOTE_AVAILABILITY_URL,
    _parse_availability,
    extract_chipset,
    fetch_app_availability,
    find_app_name,
    find_availability,
    firmware_in_range,
    pick_chipset_payload,
)

# ---------------------------------------------------------------------------
# _parse_availability
# ---------------------------------------------------------------------------


class TestParseAvailability:
    def test_basic_shape(self) -> None:
        payload = [
            {
                "id": "44",
                "chipsets": {
                    "*": [
                        {
                            "app_type_payload": (
                                '{"NAME_SPACE":5,"APP_ID":"1","MESSAGE":null}'
                            ),
                            "firmwareMinimum": None,
                            "firmwareMaximum": None,
                        }
                    ]
                },
            }
        ]
        result = _parse_availability(payload)
        assert len(result) == 1
        entry = result[0]
        assert entry.app_id == "44"
        assert "*" in entry.chipsets
        payloads = entry.chipsets["*"]
        assert payloads[0].config == AppConfig(app_id="1", name_space=5, message=None)

    def test_skips_null_id(self) -> None:
        # The live endpoint includes 2 entries with id=null that can't
        # be joined to the catalog — they must be dropped, not coerced.
        payload = [
            {
                "id": None,
                "chipsets": {
                    "*": [
                        {
                            "app_type_payload": '{"NAME_SPACE":2,"APP_ID":"x"}',
                            "firmwareMinimum": None,
                            "firmwareMaximum": None,
                        }
                    ]
                },
            }
        ]
        assert _parse_availability(payload) == ()

    def test_skips_malformed_payload_string(self) -> None:
        payload = [
            {
                "id": "1",
                "chipsets": {
                    "MT5583": [
                        {"app_type_payload": "not json", "firmwareMinimum": None}
                    ]
                },
            }
        ]
        # The whole entry has no usable payloads → dropped (preserving the
        # invariant that an AppAvailability always carries at least one
        # chipset).
        assert _parse_availability(payload) == ()

    def test_preserves_firmware_bounds(self) -> None:
        payload = [
            {
                "id": "23015",
                "chipsets": {
                    "MT5583": [
                        {
                            "app_type_payload": '{"NAME_SPACE":2,"APP_ID":"3015"}',
                            "firmwareMinimum": "3.520.28.1-2",
                            "firmwareMaximum": None,
                        }
                    ]
                },
            }
        ]
        result = _parse_availability(payload)
        assert result[0].chipsets["MT5583"][0].firmware_minimum == "3.520.28.1-2"
        assert result[0].chipsets["MT5583"][0].firmware_maximum == ""

    def test_non_list_root_returns_empty(self) -> None:
        assert _parse_availability({"not": "list"}) == ()
        assert _parse_availability(None) == ()

    def test_skips_non_dict_chipsets(self) -> None:
        # Whole entry must be dropped if chipsets isn't a dict, since
        # AppAvailability requires at least one chipset key.
        payload = [{"id": "1", "chipsets": "not-a-dict"}]
        assert _parse_availability(payload) == ()


# ---------------------------------------------------------------------------
# extract_chipset
# ---------------------------------------------------------------------------


class TestExtractChipset:
    def test_mediatek(self) -> None:
        assert extract_chipset("mtk5583-1.6.1312.1") == "MT5583"

    def test_novatek(self) -> None:
        assert extract_chipset("nt72690-x.y") == "NT72690"
        assert extract_chipset("ntv72690-x.y") == "NT72690"

    def test_unknown_prefix(self) -> None:
        assert extract_chipset("xyz1234-foo") is None
        assert extract_chipset("") is None
        assert extract_chipset(None) is None

    def test_l_suffix_fallback(self) -> None:
        # When MT5586 is *not* in the keys but MT5586L is, fall back to
        # the L variant. Resolves the binary-string ambiguity.
        keys = {"MT5586L", "MT5583"}
        assert extract_chipset("mtk5586-x", available_keys=keys) == "MT5586L"

    def test_prefers_exact_when_both_exist(self) -> None:
        keys = {"MT5586", "MT5586L"}
        assert extract_chipset("mtk5586-x", available_keys=keys) == "MT5586"

    def test_returns_none_when_no_match(self) -> None:
        # Bare key set with neither candidate present.
        assert extract_chipset("mtk5586-x", available_keys={"MT5583"}) is None


# ---------------------------------------------------------------------------
# firmware_in_range
# ---------------------------------------------------------------------------


class TestFirmwareInRange:
    def test_unbounded(self) -> None:
        # Empty min/max means unbounded.
        assert firmware_in_range("3.720.9.1-1") is True
        assert firmware_in_range("3.720.9.1-1", minimum="", maximum="") is True

    def test_above_minimum(self) -> None:
        assert firmware_in_range("3.720.9.1-1", minimum="3.520.28.1-2") is True

    def test_below_minimum(self) -> None:
        assert firmware_in_range("3.520.27.0", minimum="3.520.28.1-2") is False

    def test_above_maximum(self) -> None:
        assert firmware_in_range("5.0.0.0-1", maximum="3.520.28.1-2") is False

    def test_inside_range(self) -> None:
        assert (
            firmware_in_range(
                "3.520.30.0-1",
                minimum="3.520.28.1-2",
                maximum="3.521.0.0-0",
            )
            is True
        )

    def test_unparseable_is_permissive(self) -> None:
        # Older firmware may report a non-numeric version string; we
        # don't want to silently filter their apps. Permissive: assume
        # compatible when in doubt.
        assert firmware_in_range("unknown") is True
        assert firmware_in_range("unknown", minimum="3.0") is True
        assert firmware_in_range("3.0", minimum="not-a-version") is True


# ---------------------------------------------------------------------------
# pick_chipset_payload
# ---------------------------------------------------------------------------


def _entry(
    *, app_id: str, payloads_by_chipset: dict[str, list[ChipsetPayload]]
) -> AppAvailability:
    return AppAvailability(
        app_id=app_id,
        chipsets=MappingProxyType(
            {k: tuple(v) for k, v in payloads_by_chipset.items()}
        ),
    )


class TestPickChipsetPayload:
    def test_specific_chipset_wins_over_wildcard(self) -> None:
        specific = ChipsetPayload(
            config=AppConfig(app_id="1", name_space=2),
        )
        wild = ChipsetPayload(config=AppConfig(app_id="99", name_space=2))
        entry = _entry(
            app_id="x",
            payloads_by_chipset={"MT5583": [specific], "*": [wild]},
        )
        result = pick_chipset_payload(entry, chipset="MT5583", firmware="3.0.0")
        assert result is specific

    def test_falls_back_to_wildcard(self) -> None:
        wild = ChipsetPayload(config=AppConfig(app_id="9", name_space=2))
        entry = _entry(app_id="x", payloads_by_chipset={"*": [wild]})
        # Chipset has no specific entry → wildcard.
        assert pick_chipset_payload(entry, chipset="MT5586", firmware="") is wild

    def test_no_chipset_falls_back_to_wildcard(self) -> None:
        # Older firmware may not expose a chipset string.
        wild = ChipsetPayload(config=AppConfig(app_id="9", name_space=2))
        entry = _entry(app_id="x", payloads_by_chipset={"*": [wild]})
        assert pick_chipset_payload(entry, chipset=None, firmware="") is wild

    def test_skips_payload_outside_firmware_range(self) -> None:
        too_new = ChipsetPayload(
            config=AppConfig(app_id="1", name_space=2),
            firmware_minimum="5.0.0.0-1",
        )
        entry = _entry(app_id="x", payloads_by_chipset={"MT5583": [too_new]})
        # Device fw is below the minimum — no payload matches.
        assert (
            pick_chipset_payload(entry, chipset="MT5583", firmware="3.0.0.0-1") is None
        )

    def test_multiple_payloads_first_match_wins(self) -> None:
        # Variants ordered as they appear in the data — first matching
        # firmware bound wins. (Mirrors the ``for payload in …`` loop
        # order; the catalog is curated.)
        old = ChipsetPayload(
            config=AppConfig(app_id="old", name_space=2),
            firmware_maximum="3.500.0.0-0",
        )
        new = ChipsetPayload(
            config=AppConfig(app_id="new", name_space=2),
            firmware_minimum="3.500.0.0-1",
        )
        entry = _entry(app_id="x", payloads_by_chipset={"MT5583": [old, new]})
        result = pick_chipset_payload(entry, chipset="MT5583", firmware="3.700.0.0-0")
        assert result is new


# ---------------------------------------------------------------------------
# find_app_name with availability reverse lookup
# ---------------------------------------------------------------------------


class TestFindAppNameViaAvailability:
    def test_resolves_via_availability_when_no_legacy_config(self) -> None:
        # Modern catalog: AppRecord.config is empty, but availability
        # has a payload with a matching AppConfig. find_app_name should
        # reverse-lookup the id and return the catalog name.
        record = AppRecord(name="YouTube", country=("*",), id="44")
        avail = AppAvailability(
            app_id="44",
            chipsets=MappingProxyType(
                {"*": (ChipsetPayload(config=AppConfig(app_id="1", name_space=5)),)}
            ),
        )
        # Device reports current app config matching the availability payload.
        config = AppConfig(app_id="1", name_space=5)
        assert find_app_name(config, [record], availability=[avail]) == "YouTube"

    def test_legacy_config_still_wins(self) -> None:
        # If the catalog still has a legacy config[0] that matches, it
        # short-circuits before the availability fallback fires.
        record = AppRecord(
            name="LegacyApp",
            country=("*",),
            config=(AppConfig(app_id="1", name_space=2),),
            id="42",
        )
        avail = AppAvailability(
            app_id="42",
            chipsets=MappingProxyType(
                {"*": (ChipsetPayload(config=AppConfig(app_id="999", name_space=99)),)}
            ),
        )
        # Match via the legacy config field, not via availability.
        config = AppConfig(app_id="1", name_space=2)
        assert find_app_name(config, [record], availability=[avail]) == "LegacyApp"

    def test_unknown_with_availability_returns_none(self) -> None:
        record = AppRecord(name="X", country=("*",), id="1")
        avail = AppAvailability(
            app_id="1",
            chipsets=MappingProxyType(
                {"*": (ChipsetPayload(config=AppConfig(app_id="1", name_space=2)),)}
            ),
        )
        config = AppConfig(app_id="999", name_space=99)
        assert find_app_name(config, [record], availability=[avail]) is None


# ---------------------------------------------------------------------------
# find_availability helper
# ---------------------------------------------------------------------------


class TestFindAvailability:
    def test_match_by_id(self) -> None:
        a = AppAvailability(app_id="1", chipsets=MappingProxyType({}))
        b = AppAvailability(app_id="2", chipsets=MappingProxyType({}))
        assert find_availability("2", [a, b]) is b

    def test_no_match(self) -> None:
        a = AppAvailability(app_id="1", chipsets=MappingProxyType({}))
        assert find_availability("999", [a]) is None


# ---------------------------------------------------------------------------
# fetch_app_availability with bundled fallback
# ---------------------------------------------------------------------------


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


class TestFetchAppAvailability:
    async def test_remote_success(self, session: aiohttp.ClientSession) -> None:
        payload = [
            {
                "id": "999",
                "chipsets": {
                    "*": [
                        {
                            "app_type_payload": ('{"NAME_SPACE":2,"APP_ID":"x"}'),
                            "firmwareMinimum": None,
                            "firmwareMaximum": None,
                        }
                    ]
                },
            }
        ]
        with aioresponses() as m:
            m.get(REMOTE_AVAILABILITY_URL, payload=payload)
            result = await fetch_app_availability(session=session)
        ids = {entry.app_id for entry in result}
        assert "999" in ids

    async def test_remote_500_falls_back_to_bundle(
        self, session: aiohttp.ClientSession
    ) -> None:
        with aioresponses() as m:
            m.get(REMOTE_AVAILABILITY_URL, status=500)
            result = await fetch_app_availability(session=session)
        assert result == BUNDLED_AVAILABILITY

    async def test_malformed_json_falls_back(
        self, session: aiohttp.ClientSession
    ) -> None:
        with aioresponses() as m:
            m.get(REMOTE_AVAILABILITY_URL, body="not json")
            result = await fetch_app_availability(session=session)
        assert result == BUNDLED_AVAILABILITY

    async def test_never_raises(self, session: aiohttp.ClientSession) -> None:
        with aioresponses() as m:
            m.get(REMOTE_AVAILABILITY_URL, exception=RuntimeError("boom"))
            result = await fetch_app_availability(session=session)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Bundled availability sanity
# ---------------------------------------------------------------------------


class TestBundledAvailability:
    def test_non_empty(self) -> None:
        assert len(BUNDLED_AVAILABILITY) > 0

    def test_known_app_present(self) -> None:
        # YouTube has id=44 in the modern catalog; the bundled
        # availability snapshot should include it.
        assert find_availability("44", BUNDLED_AVAILABILITY) is not None

    def test_chipset_keys_make_sense(self) -> None:
        seen = {k for entry in BUNDLED_AVAILABILITY for k in entry.chipsets}
        # At minimum the wildcard and one specific MediaTek chipset.
        assert "*" in seen
        assert any(k.startswith("MT") for k in seen)
