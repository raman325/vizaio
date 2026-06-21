"""Unit tests for ``discovery`` private helpers.

These cover the decode/parse functions that the higher-level
``discover_zeroconf`` / ``discover_ssdp`` flows depend on. Keeping
them as pure-function tests means we don't need real sockets or
real zeroconf to exercise the surrounding edge cases.
"""

from __future__ import annotations

from types import SimpleNamespace
from xml.etree import ElementTree

from vizaio.discovery import (
    _decode_id_property,
    _decode_property,
    _find_first,
    _find_text,
    _local_name,
    _merge_by_ip,
    _split_location,
    _ssdp_to_device,
    _SsdpProtocol,
    _zeroconf_to_device,
)
from vizaio.types import DiscoveredDevice

# ---------------------------------------------------------------------------
# _decode_property (TXT record string field)
# ---------------------------------------------------------------------------


class TestDecodeProperty:
    def test_utf8_bytes(self) -> None:
        assert _decode_property(b"VHD24M-0810") == "VHD24M-0810"

    def test_invalid_utf8_falls_back_to_hex(self) -> None:
        # \xff\xfe is not a valid UTF-8 start sequence — should hex-encode.
        assert _decode_property(b"\xff\xfe") == "fffe"

    def test_none_returns_empty_string(self) -> None:
        assert _decode_property(None) == ""

    def test_str_passes_through(self) -> None:
        assert _decode_property("plain") == "plain"


# ---------------------------------------------------------------------------
# _decode_id_property — UUID-like, hex-validated
# ---------------------------------------------------------------------------


class TestDecodeIdProperty:
    def test_hex_string_bytes(self) -> None:
        assert _decode_id_property(b"abc123") == "abc123"

    def test_non_hex_returns_hex_encoded(self) -> None:
        # ``b"hello"`` is valid UTF-8 but not hex — fall back to hex form.
        assert _decode_id_property(b"hello") == b"hello".hex()

    def test_invalid_utf8_returns_hex(self) -> None:
        assert _decode_id_property(b"\xff\xfe") == "fffe"

    def test_none_returns_empty(self) -> None:
        assert _decode_id_property(None) == ""

    def test_str_returns_str(self) -> None:
        assert _decode_id_property("deadbeef") == "deadbeef"


# ---------------------------------------------------------------------------
# _zeroconf_to_device — TXT/address parsing
# ---------------------------------------------------------------------------


def _info(
    *,
    name: str = "Living Room TV._viziocast._tcp.local.",
    addresses: list[str] | None = None,
    port: int = 7345,
    properties: dict[bytes, bytes] | None = None,
    parsed_addresses_raises: BaseException | None = None,
) -> SimpleNamespace:
    """Duck-typed ``AsyncServiceInfo`` for ``_zeroconf_to_device``."""
    if addresses is None:
        addresses = ["192.0.2.10"]
    if properties is None:
        properties = {}

    def _parsed_addresses(*args: object) -> list[str]:
        if parsed_addresses_raises is not None:
            raise parsed_addresses_raises
        return addresses

    return SimpleNamespace(
        name=name,
        port=port,
        properties=properties,
        parsed_addresses=_parsed_addresses,
    )


class TestZeroconfToDevice:
    def test_full_record(self) -> None:
        device = _zeroconf_to_device(
            _info(
                addresses=["192.0.2.10"],
                port=7345,
                properties={
                    b"name": b"VHD24M-0810",
                    b"id": b"abc123",
                },
            )
        )
        assert device is not None
        assert device.ip == "192.0.2.10"
        assert device.port == 7345
        assert device.model == "VHD24M-0810"
        assert device.id == "abc123"

    def test_strips_service_type_suffix(self) -> None:
        device = _zeroconf_to_device(_info(name="My TV._viziocast._tcp.local."))
        assert device is not None
        assert device.name == "My TV"

    def test_strips_unterminated_service_type(self) -> None:
        # No trailing dot — second branch of the suffix-strip.
        device = _zeroconf_to_device(_info(name="My TV._viziocast._tcp.local"))
        assert device is not None
        assert device.name == "My TV"

    def test_no_addresses_returns_none(self) -> None:
        # Empty address list → can't resolve → None.
        assert _zeroconf_to_device(_info(addresses=[])) is None

    def test_attribute_error_on_parsed_addresses_falls_back(self) -> None:
        # SimpleNamespace lambdas accept any args, but tests can pass
        # a stricter parsed_addresses that raises TypeError when called
        # with a positional IPVersion arg — exercise the fallback branch.
        info = _info(
            addresses=["192.0.2.20"],
            parsed_addresses_raises=TypeError("unexpected arg"),
        )

        # _zeroconf_to_device first tries with IPVersion.V4Only; we want
        # the second call (positional-free) to succeed. Patch through
        # SimpleNamespace by giving parsed_addresses a function that
        # raises only with args.
        def _addrs(*args: object) -> list[str]:
            if args:
                raise TypeError("unexpected arg")
            return ["192.0.2.20"]

        info.parsed_addresses = _addrs
        device = _zeroconf_to_device(info)
        assert device is not None
        assert device.ip == "192.0.2.20"


# ---------------------------------------------------------------------------
# _SsdpProtocol — datagram parsing without real sockets
# ---------------------------------------------------------------------------


class TestSsdpProtocol:
    def test_collects_location_header(self) -> None:
        proto = _SsdpProtocol()
        # Realistic SSDP response.
        proto.datagram_received(
            (
                b"HTTP/1.1 200 OK\r\n"
                b"CACHE-CONTROL: max-age=1800\r\n"
                b"LOCATION: http://192.0.2.50:8008/ssdp/device-desc.xml\r\n"
                b"SERVER: Linux UPnP/1.0\r\n\r\n"
            ),
            ("192.0.2.50", 1900),
        )
        assert proto.locations == ["http://192.0.2.50:8008/ssdp/device-desc.xml"]

    def test_only_first_location_per_response(self) -> None:
        # Defensive: the function ``return``s after the first LOCATION.
        proto = _SsdpProtocol()
        proto.datagram_received(
            (
                b"HTTP/1.1 200 OK\r\n"
                b"LOCATION: http://192.0.2.50:8008/a\r\n"
                b"LOCATION: http://192.0.2.50:8008/b\r\n\r\n"
            ),
            ("192.0.2.50", 1900),
        )
        assert proto.locations == ["http://192.0.2.50:8008/a"]

    def test_response_without_location_is_dropped(self) -> None:
        proto = _SsdpProtocol()
        proto.datagram_received(
            b"HTTP/1.1 200 OK\r\nSERVER: foo\r\n\r\n",
            ("1.1.1.1", 1900),
        )
        assert proto.locations == []

    def test_invalid_utf8_does_not_raise(self) -> None:
        proto = _SsdpProtocol()
        # ``decode(errors="replace")`` lets garbage bytes through as ?
        proto.datagram_received(b"\xff\xfe\x00", ("1.1.1.1", 1900))
        assert proto.locations == []


# ---------------------------------------------------------------------------
# _ssdp_to_device — XML parsing
# ---------------------------------------------------------------------------


def _xml(manufacturer: str = "VIZIO", **fields: str) -> bytes:
    """Build a minimal DIAL device XML."""
    parts = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return (
        f'<?xml version="1.0"?>'
        f'<root xmlns="urn:schemas-upnp-org:device-1-0">'
        f"<device>"
        f"<manufacturer>{manufacturer}</manufacturer>"
        f"{parts}"
        f"</device></root>"
    ).encode()


class TestSsdpToDevice:
    def test_full_record(self) -> None:
        d = _ssdp_to_device(
            "http://192.0.2.10:8008/dd.xml",
            _xml(
                friendlyName="Living Room",
                modelName="VHD24M-0810",
                UDN="uuid:abc",
            ),
        )
        assert d is not None
        assert d.ip == "192.0.2.10"
        assert d.port == 8008
        assert d.name == "Living Room"
        assert d.model == "VHD24M-0810"
        assert d.id == "uuid:abc"

    def test_non_vizio_returns_none(self) -> None:
        d = _ssdp_to_device(
            "http://192.0.2.10:8008/dd.xml",
            _xml(manufacturer="Google Inc.", friendlyName="Chromecast"),
        )
        assert d is None

    def test_invalid_xml_returns_none(self) -> None:
        assert _ssdp_to_device("http://x:1/", b"not xml<<<") is None

    def test_xml_without_device_element_returns_none(self) -> None:
        # Manufacturer-less, device-less root.
        assert _ssdp_to_device("http://x:1/", b'<?xml version="1.0"?><root/>') is None

    def test_location_without_host_returns_none(self) -> None:
        # ``urlsplit`` of ``"/no-scheme"`` has no hostname → return None.
        assert (
            _ssdp_to_device(
                "/no-scheme",
                _xml(friendlyName="Vizio"),
            )
            is None
        )

    def test_missing_friendly_name_falls_back(self) -> None:
        d = _ssdp_to_device(
            "http://192.0.2.10:8008/",
            _xml(modelName="x"),
        )
        assert d is not None
        assert d.name == "Vizio device"


# ---------------------------------------------------------------------------
# Helpers: _find_first, _find_text, _local_name, _split_location
# ---------------------------------------------------------------------------


class TestSplitLocation:
    def test_with_port(self) -> None:
        assert _split_location("http://192.0.2.10:8008/x") == ("192.0.2.10", 8008)

    def test_without_port_zero(self) -> None:
        # urlsplit returns None for missing port; we coerce to 0.
        assert _split_location("http://192.0.2.10/") == ("192.0.2.10", 0)

    def test_no_host(self) -> None:
        host, port = _split_location("/just-a-path")
        assert host is None


class TestLocalName:
    def test_strips_namespace(self) -> None:
        assert _local_name("{urn:schemas-upnp-org:device-1-0}device") == "device"

    def test_no_namespace(self) -> None:
        assert _local_name("device") == "device"


class TestFindHelpers:
    def test_find_first_returns_none_when_absent(self) -> None:
        root = ElementTree.fromstring(b"<root><a/></root>")
        assert _find_first(root, "missing") is None

    def test_find_text_empty_when_absent(self) -> None:
        root = ElementTree.fromstring(b"<root><a/></root>")
        assert _find_text(root, "missing") == ""

    def test_find_text_strips_whitespace(self) -> None:
        root = ElementTree.fromstring(b"<root><a>  hello  </a></root>")
        assert _find_text(root, "a") == "hello"

    def test_find_text_handles_empty_element(self) -> None:
        root = ElementTree.fromstring(b"<root><a></a></root>")
        assert _find_text(root, "a") == ""


# ---------------------------------------------------------------------------
# _merge_by_ip — dedupe behavior
# ---------------------------------------------------------------------------


class TestMergeByIp:
    def test_primary_wins_on_dup(self) -> None:
        primary = [DiscoveredDevice(name="A-zc", ip="1.1.1.1", port=7345, model="x")]
        secondary = [
            DiscoveredDevice(name="A-ssdp", ip="1.1.1.1", port=8008, model="x")
        ]
        merged = _merge_by_ip(primary, secondary)
        assert len(merged) == 1
        assert merged[0].name == "A-zc"

    def test_distinct_ips_preserved(self) -> None:
        primary = [DiscoveredDevice(name="A", ip="1.1.1.1", port=7345, model="x")]
        secondary = [DiscoveredDevice(name="B", ip="2.2.2.2", port=8008, model="y")]
        merged = _merge_by_ip(primary, secondary)
        assert {d.ip for d in merged} == {"1.1.1.1", "2.2.2.2"}

    def test_empty_inputs(self) -> None:
        assert _merge_by_ip([], []) == []

    def test_only_primary(self) -> None:
        primary = [DiscoveredDevice(name="A", ip="1.1.1.1", port=7345, model="x")]
        assert _merge_by_ip(primary, []) == primary
