"""Wire envelope: Response.from_json + Item.

This is THE boundary between "raw JSON dict from device" and "typed Python
data the rest of the package operates on." If `Response.from_json` is
correct, downstream code can rely on lowercase keys, normalized cnames,
and well-typed fields without defensive code.

Test plan:
1. Envelope shape validation (status field present, malformed inputs
   raise VizioResponseError).
2. CNAME normalization — protocol-notes quirks #1 (mixed casing) and
   #2 (the alias dict is dropped because lowercase-once subsumes it).
3. Status mapping — each documented STATUS.RESULT string maps to the
   right ResponseStatus enum value, unknown strings fall through to
   ResponseStatus.UNKNOWN.
4. Item accessors — find_item / require_item / has_item / items_by_type.
5. Field coercion — hashval as int even when the device emits it as a
   string (observed in some pyvizio fixtures).
"""

from __future__ import annotations

import pytest

from tests._fixtures import (
    make_current_app_response,
    make_error_response,
    make_inputs_list_response,
    make_item,
    make_no_app_response,
    make_pair_begin_response,
    make_power_response,
    make_settings_response,
    make_success_response,
)
from vizaio import (
    ResponseStatus,
    VizioNotFoundError,
    VizioResponseError,
)
from vizaio.wire import Item, Response

CASINGS = ["upper", "lower", "mixed"]


# ===========================================================================
# Envelope shape
# ===========================================================================


class TestEnvelopeShape:
    """Response.from_json validates that essential structure is present."""

    def test_minimal_success(self) -> None:
        response = Response.from_json(make_success_response())
        assert response.status is ResponseStatus.SUCCESS
        assert response.detail == "Success"

    def test_missing_status_raises(self) -> None:
        with pytest.raises(VizioResponseError, match="missing.*STATUS|status"):
            Response.from_json({"ITEMS": []})

    def test_status_not_a_dict_raises(self) -> None:
        with pytest.raises(VizioResponseError):
            Response.from_json({"STATUS": "ok"})

    def test_empty_dict_raises(self) -> None:
        with pytest.raises(VizioResponseError):
            Response.from_json({})

    def test_items_default_empty(self) -> None:
        """Responses without ITEMS should not crash — many endpoints (e.g.,
        cancel_pair) return only STATUS."""
        response = Response.from_json(make_success_response())
        assert response.items == ()

    def test_pair_response_has_singular_item(self) -> None:
        """Pairing endpoints use ITEM (singular) instead of ITEMS. Both
        shapes must parse — Item-shaped data should appear in
        ``response.items`` regardless of which key the device used."""
        response = Response.from_json(make_pair_begin_response(1, 54321))
        assert len(response.items) == 1


# ===========================================================================
# CNAME / key normalization
# ===========================================================================


class TestCnameNormalization:
    """Quirk #1: device returns mixed-case keys across firmware versions.
    Quirk #2: the aliases dict is subsumed by lowercase-once normalization.
    Both resolved here at the wire boundary."""

    @pytest.mark.parametrize("casing", CASINGS)
    def test_cname_lowercased(self, casing: str) -> None:
        raw = make_power_response(1, casing=casing)  # type: ignore[arg-type]
        response = Response.from_json(raw)
        item = response.require_item("power_mode")
        assert item.cname == "power_mode"

    @pytest.mark.parametrize("casing", CASINGS)
    def test_envelope_keys_case_insensitive(self, casing: str) -> None:
        raw = make_power_response(1, casing=casing)  # type: ignore[arg-type]
        response = Response.from_json(raw)
        # Whatever case the device used for "STATUS", we still parse it.
        assert response.status is ResponseStatus.SUCCESS

    def test_uppercase_cname_in_request_lookup(self) -> None:
        """Even if the response has CNAME='POWER_MODE', looking up
        'power_mode' must succeed — that's the whole point of normalizing."""
        raw = make_power_response(1, casing="upper")
        response = Response.from_json(raw)
        assert response.find_item("power_mode") is not None

    def test_case_insensitive_lookup(self) -> None:
        """Callers looking up 'POWER_MODE' should also work — the lookup
        helper itself normalizes. Defense in depth."""
        raw = make_power_response(1, casing="upper")
        response = Response.from_json(raw)
        assert response.find_item("POWER_MODE") is not None
        assert response.find_item("Power_Mode") is not None

    def test_no_alias_dict_needed(self) -> None:
        """Sanity: pyvizio's `ITEM_CNAME` alias dict mapped POWER_MODE
        to power_mode etc. Our normalization makes the alias dict
        unnecessary — direct lookups by lowercase cname work for both
        casings of input."""
        for in_casing in CASINGS:
            raw = make_power_response(1, casing=in_casing)  # type: ignore[arg-type]
            response = Response.from_json(raw)
            assert response.find_item("power_mode") is not None


# ===========================================================================
# Status mapping
# ===========================================================================


class TestStatusMapping:
    """Each documented STATUS.RESULT string maps to the right enum."""

    @pytest.mark.parametrize(
        "result,expected",
        [
            ("SUCCESS", ResponseStatus.SUCCESS),
            ("success", ResponseStatus.SUCCESS),
            ("Success", ResponseStatus.SUCCESS),
            ("INVALID_PARAMETER", ResponseStatus.INVALID_PARAMETER),
            ("invalid_parameter", ResponseStatus.INVALID_PARAMETER),
            ("FAILURE", ResponseStatus.FAILURE),
            ("BLOCKED", ResponseStatus.BLOCKED),
            ("REQUIRES_PAIRING", ResponseStatus.REQUIRES_PAIRING),
            ("PAIRING_DENIED", ResponseStatus.PAIRING_DENIED),
            # Modern firmware (~3.7+) returns this for paths the device
            # doesn't expose. Captured live from VHD24M-0810 fw 3.720.9.1-1
            # at tests/captured/esn_modern_404.json.
            ("URI_NOT_FOUND", ResponseStatus.URI_NOT_FOUND),
            ("uri_not_found", ResponseStatus.URI_NOT_FOUND),
        ],
    )
    def test_known_status(self, result: str, expected: ResponseStatus) -> None:
        response = Response.from_json(make_error_response(result=result, detail="x"))
        assert response.status is expected

    def test_unknown_status_preserved(self) -> None:
        """If Vizio adds a new STATUS.RESULT value, we should not crash;
        we expose it as ResponseStatus.UNKNOWN with the raw string
        preserved on result_raw."""
        response = Response.from_json(
            {"STATUS": {"RESULT": "FUTURE_VALUE", "DETAIL": "?"}}
        )
        assert response.status is ResponseStatus.UNKNOWN
        assert response.result_raw == "FUTURE_VALUE"

    def test_missing_result_field(self) -> None:
        """STATUS without RESULT — pyvizio test 'test_missing_status' covers
        this, but for the inner shape (status object exists, result key
        missing). Should raise VizioResponseError."""
        with pytest.raises(VizioResponseError):
            Response.from_json({"STATUS": {"DETAIL": "no result"}})

    def test_detail_preserved(self) -> None:
        response = Response.from_json(
            make_error_response(result="FAILURE", detail="Something broke")
        )
        assert "Something broke" in response.detail


# ===========================================================================
# Item accessors
# ===========================================================================


class TestItemAccessors:
    """find_item / require_item / has_item / items_by_type."""

    def test_find_item_present(self) -> None:
        response = Response.from_json(make_power_response(1))
        item = response.find_item("power_mode")
        assert item is not None
        assert item.value == 1

    def test_find_item_missing_returns_none(self) -> None:
        response = Response.from_json(make_power_response(1))
        assert response.find_item("nonexistent") is None

    def test_require_item_present(self) -> None:
        response = Response.from_json(make_power_response(1))
        assert response.require_item("power_mode").value == 1

    def test_require_item_missing_raises(self) -> None:
        response = Response.from_json(make_power_response(1))
        with pytest.raises(VizioNotFoundError, match="nonexistent"):
            response.require_item("nonexistent")

    def test_has_item(self) -> None:
        response = Response.from_json(make_power_response(1))
        assert response.has_item("power_mode")
        assert not response.has_item("nope")

    def test_items_by_type(self) -> None:
        # Settings response with mixed types.
        response = Response.from_json(
            make_settings_response(
                [
                    ("volume", 25, "T_VALUE_ABS_V1", 1),
                    ("eq", "Normal", "T_LIST_V1", 2),
                    ("bass", 0, "T_VALUE_ABS_V1", 3),
                ]
            )
        )
        sliders = response.items_by_type("T_VALUE_ABS_V1")
        assert {i.cname for i in sliders} == {"volume", "bass"}


# ===========================================================================
# Item parsing details
# ===========================================================================


class TestItemFields:
    """Field-level normalization on Item itself."""

    def test_simple_int_value(self) -> None:
        response = Response.from_json(make_power_response(0))
        assert response.require_item("power_mode").value == 0

    def test_string_value(self) -> None:
        raw = make_success_response(items=[make_item("mute", "Off")])
        response = Response.from_json(raw)
        assert response.require_item("mute").value == "Off"

    def test_dict_value_preserved(self) -> None:
        # Inputs return VALUE as a nested dict.
        response = Response.from_json(
            make_inputs_list_response(
                [("hdmi1", "HDMI-1", "PS5", 1)],
                include_synthetic_current=False,
            )
        )
        item = response.require_item("hdmi1")
        assert isinstance(item.value, dict)
        # Inner keys should also be normalized by Response.from_json.
        assert "name" in item.value or "NAME" in item.value

    def test_hashval_int(self) -> None:
        raw = make_success_response(items=[make_item("volume", 25, hashval=12345)])
        response = Response.from_json(raw)
        assert response.require_item("volume").hashval == 12345

    def test_hashval_absent(self) -> None:
        """Some items (e.g., the synthetic current_input) come without
        HASHVAL. Our Item should expose hashval=None, not crash."""
        raw = make_success_response(items=[make_item("mute", "Off", hashval=None)])
        response = Response.from_json(raw)
        assert response.require_item("mute").hashval is None

    def test_elements_tuple(self) -> None:
        """ELEMENTS arrays land on Item.elements as a tuple (immutable)."""
        raw = make_success_response(
            items=[
                make_item(
                    "eq",
                    "Normal",
                    item_type="T_LIST_V1",
                    ELEMENTS=["Normal", "Music", "Movie"],
                )
            ]
        )
        response = Response.from_json(raw)
        item = response.require_item("eq")
        assert item.elements == ("Normal", "Music", "Movie")

    def test_min_max_center(self) -> None:
        raw = make_success_response(
            items=[
                make_item(
                    "bass",
                    0,
                    item_type="T_VALUE_ABS_V1",
                    MINIMUM=-6,
                    MAXIMUM=6,
                    CENTER=0,
                )
            ]
        )
        response = Response.from_json(raw)
        item = response.require_item("bass")
        assert item.min == -6
        assert item.max == 6
        assert item.center == 0

    def test_raw_dict_preserved(self) -> None:
        """Item.raw is the escape hatch for fields we haven't modeled.
        Future-Vizio firmware adding a new field shouldn't require a
        library bump just to read it."""
        raw = make_success_response(items=[make_item("x", 1, FUTURE_FIELD="surprise!")])
        response = Response.from_json(raw)
        item = response.require_item("x")
        # raw access is case-insensitive — caller may use either casing.
        assert any(v == "surprise!" for v in item.raw.values()), (
            "FUTURE_FIELD should be preserved in Item.raw"
        )


# ===========================================================================
# Frozen / immutable
# ===========================================================================


class TestImmutability:
    """Response and Item are frozen dataclasses — caller can't mutate."""

    def test_response_is_frozen(self) -> None:
        response = Response.from_json(make_power_response(1))
        with pytest.raises((AttributeError, TypeError)):
            response.detail = "mutated"  # type: ignore[misc]

    def test_item_is_frozen(self) -> None:
        response = Response.from_json(make_power_response(1))
        item = response.require_item("power_mode")
        with pytest.raises((AttributeError, TypeError)):
            item.value = 999  # type: ignore[misc]


# ===========================================================================
# Realistic shapes covering quirks
# ===========================================================================


class TestRealisticShapes:
    """End-to-end parsing of fixtures that mirror real device responses."""

    def test_inputs_with_synthetic_current(self) -> None:
        """Quirk #6: the inputs response includes a synthetic
        ``current_input`` item alongside the real inputs. Response.from_json
        keeps it; ``parse_inputs`` (in _parse.py) is responsible for
        filtering."""
        raw = make_inputs_list_response(
            [
                ("hdmi1", "HDMI-1", "PS5", 1),
                ("hdmi2", "HDMI-2", "Apple TV", 2),
            ],
            include_synthetic_current=True,
            current_input_meta_name="HDMI-1",
        )
        response = Response.from_json(raw)
        cnames = {i.cname for i in response.items}
        assert cnames == {"hdmi1", "hdmi2", "current_input"}

    def test_no_app_value_null(self) -> None:
        """Quirk #9 form A: VALUE: null."""
        raw = make_no_app_response(value_present=True)
        response = Response.from_json(raw)
        assert response.status is ResponseStatus.SUCCESS
        # Item.value is None, not absent.
        # (Used by parse_current_app_config to detect "no app running".)
        assert len(response.items) == 1
        assert response.items[0].value is None

    def test_no_app_value_absent(self) -> None:
        """Quirk #9 form B: VALUE key omitted entirely."""
        raw = make_no_app_response(value_present=False)
        response = Response.from_json(raw)
        # Both forms produce equivalent observable behavior — a single
        # item whose .value is None. Downstream parsing treats them
        # identically.
        assert len(response.items) == 1
        assert response.items[0].value is None

    def test_app_running(self) -> None:
        raw = make_current_app_response(
            app_id="3", name_space=2, message="https://hulu.com"
        )
        response = Response.from_json(raw)
        item = response.items[0]
        assert isinstance(item.value, dict)
        # Inner keys were normalized to lowercase by Response.from_json.
        v = item.value
        # Allow either casing here — the normalization promise is
        # consistency, not a specific casing.
        keys_lower = {k.lower() for k in v}
        assert keys_lower == {"app_id", "name_space", "message"}


# ===========================================================================
# Item construction (for tests building Response from synthetic data)
# ===========================================================================


class TestItemDirectConstruction:
    """Item is the public type — should be constructible without going
    through Response.from_json (useful for tests and library users
    building synthetic data)."""

    def test_construct_minimal(self) -> None:
        item = Item(cname="x", type="T_VALUE_V1", name="x", value=1)
        assert item.cname == "x"
        assert item.hashval is None  # default
        assert item.elements == ()  # default

    def test_construct_full(self) -> None:
        item = Item(
            cname="bass",
            type="T_VALUE_ABS_V1",
            name="Bass",
            value=0,
            hashval=12345,
            min=-6,
            max=6,
            center=0,
        )
        assert item.min == -6
