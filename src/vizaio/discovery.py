"""
Network discovery for Vizio devices.

Public API:

- :func:`discover` — run zeroconf + SSDP concurrently, dedupe by IP
- :func:`discover_zeroconf` — mDNS only (requires the ``[discovery]`` extra)
- :func:`discover_ssdp` — SSDP/DIAL fallback (no extra dependency)

Per protocol-notes #27 (APK confirmed): the official Vizio app uses
zeroconf only with service type ``_viziocast._tcp``. We keep SSDP as an
opt-in fallback because pyvizio's history shows older soundbars
sometimes respond to DIAL but don't advertise mDNS reliably.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import logging
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from xml.etree import ElementTree
from xml.etree.ElementTree import ParseError

import aiohttp

from ._device import Vizio
from .endpoints import Endpoint
from .errors import VizioError
from .parse import parse_system_info_model_name
from .types import DeviceType, DiscoveredDevice

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

ZEROCONF_SERVICE_TYPE = "_viziocast._tcp.local."
"""mDNS service type advertised by Vizio devices.

Confirmed by APK ``VizioServiceDiscoveryClientKt.SERVICE_TYPE``.
"""

SSDP_TARGET = "urn:dial-multiscreen-org:device:dial:1"
"""SSDP search target — generic DIAL service. We filter by manufacturer
in the description XML since DIAL is also used by Chromecasts, Roku, etc."""

SSDP_GROUP = ("239.255.255.250", 1900)

DEFAULT_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# zeroconf — gated on the [discovery] extra
# ---------------------------------------------------------------------------

try:
    import zeroconf  # noqa: F401
    from zeroconf import IPVersion
    from zeroconf.asyncio import (
        AsyncServiceBrowser,
        AsyncServiceInfo,
        AsyncZeroconf,
    )

    _ZEROCONF_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised when extra not installed
    _ZEROCONF_AVAILABLE = False


_ZEROCONF_INSTALL_HINT = (
    "Network discovery via zeroconf requires the optional extra. "
    "Install with: pip install 'vizaio[discovery]'"
)


async def discover_zeroconf(timeout: float = DEFAULT_TIMEOUT) -> list[DiscoveredDevice]:
    """
    Discover Vizio devices via mDNS.

    Returns a list of :class:`DiscoveredDevice`. Empty list if no devices
    respond within ``timeout`` seconds.

    Raises :class:`ImportError` with an install hint if the
    ``[discovery]`` extra is not installed.
    """
    if not _ZEROCONF_AVAILABLE:
        raise ImportError(_ZEROCONF_INSTALL_HINT)

    services = await _zeroconf_scan(timeout)
    results: list[DiscoveredDevice] = []
    for info in services:
        device = _zeroconf_to_device(info)
        if device is not None:
            results.append(device)
    return results


async def _zeroconf_scan(timeout: float) -> list[Any]:
    """
    Run the AsyncZeroconf scan and collect resolved services.

    Patched in tests to feed canned :class:`AsyncServiceInfo` objects
    through the consumer without doing real mDNS.
    """
    aiozc = AsyncZeroconf()
    discovered: list[Any] = []
    try:
        # AsyncServiceBrowser requires a handler to drive resolution, but we
        # walk the cache ourselves below — so the handler is a pure no-op.
        # ``lambda *_, **__: None`` is intentional: zeroconf calls handlers
        # with keyword arguments whose names have changed across releases
        # (positional ``zc, type_, name, state_change`` → keyword
        # ``zeroconf=, service_type=, name=, state_change=``). Accepting
        # everything insulates us from future kwarg renames.
        browser = AsyncServiceBrowser(
            aiozc.zeroconf,
            ZEROCONF_SERVICE_TYPE,
            handlers=[lambda *_, **__: None],
        )
        await asyncio.sleep(timeout)
        await browser.async_cancel()

        # Walk the cache for resolved services.
        for service_name in aiozc.zeroconf.cache.names():
            if not service_name.endswith(ZEROCONF_SERVICE_TYPE):
                continue
            info = AsyncServiceInfo(ZEROCONF_SERVICE_TYPE, service_name)
            if await info.async_request(aiozc.zeroconf, timeout=1000):
                discovered.append(info)
    finally:
        await aiozc.async_close()
    return discovered


def _zeroconf_to_device(info: Any) -> DiscoveredDevice | None:
    """Convert one ``AsyncServiceInfo`` into a :class:`DiscoveredDevice`."""
    try:
        addresses = info.parsed_addresses(IPVersion.V4Only)
    except (AttributeError, TypeError):
        # Tests pass a SimpleNamespace with parsed_addresses as a lambda.
        addresses = info.parsed_addresses()
    if not addresses:
        return None

    # Display name: strip the service-type suffix from the full name.
    name = info.name
    suffix = "." + ZEROCONF_SERVICE_TYPE.rstrip(".")
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    elif name.endswith(ZEROCONF_SERVICE_TYPE):
        name = name[: -len(ZEROCONF_SERVICE_TYPE)].rstrip(".")

    properties = info.properties or {}
    model = _decode_property(properties.get(b"name", b""))
    id_value = _decode_id_property(properties.get(b"id"))
    # Vizio TXT records advertise WebSocket ports under abbreviated keys
    # ``wp`` (insecure) and ``wsp`` (secure). The ``info.port`` is the
    # HTTPS REST port; the WS server runs on a separate port (when it
    # runs at all — see protocol-notes #28; on some firmware the ports
    # are advertised but no WS server is actually listening).
    ws_port = _decode_int_property(properties.get(b"wp"))
    wss_port = _decode_int_property(properties.get(b"wsp"))

    return DiscoveredDevice(
        name=name,
        ip=addresses[0],
        port=info.port or 0,
        model=model,
        id=id_value,
        ws_port=ws_port,
        wss_port=wss_port,
    )


def _decode_int_property(value: Any) -> int | None:
    """Decode a TXT record property as an integer, or ``None`` on failure."""
    if value is None:
        return None
    try:
        text = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        return int(text)
    except (UnicodeDecodeError, ValueError):
        return None


def _decode_property(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return str(value or "")


def _decode_id_property(value: Any) -> str:
    """
    Decode an mDNS ``id`` property.

    The byte string may be UTF-8 hex (e.g. ``b"abc123"``) or non-printable;
    fall back to hex-encoded form when UTF-8 decoding fails.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            decoded = value.decode("utf-8")
            int(decoded, 16)  # validate hex-ness
            return decoded
        except (UnicodeDecodeError, ValueError):
            return value.hex()
    return str(value)


# ---------------------------------------------------------------------------
# SSDP — uses stdlib + already-required aiohttp, no extra dependency
# ---------------------------------------------------------------------------


async def discover_ssdp(timeout: float = DEFAULT_TIMEOUT) -> list[DiscoveredDevice]:
    """
    Discover Vizio devices via SSDP/DIAL.

    SSDP returns DIAL-compatible devices (TVs, Chromecasts, Rokus, etc.);
    we filter by manufacturer == "VIZIO" in the device description XML.

    Empty list on no responses or all-non-Vizio responses.
    """
    responses = await _ssdp_msearch(SSDP_TARGET, timeout=timeout)
    locations = sorted({r.location for r in responses if r.location})
    results: list[DiscoveredDevice] = []
    async with aiohttp.ClientSession() as session:
        for location in locations:
            try:
                xml = await _fetch_dial_xml(session, location, timeout=timeout)
            except Exception as e:  # network errors, malformed XML, timeouts
                _LOGGER.debug("SSDP description fetch %s failed: %s", location, e)
                continue
            device = _ssdp_to_device(location, xml)
            if device is not None:
                results.append(device)
    return results


class _SsdpResponse:
    """Minimal SSDP M-SEARCH response — only ``location`` matters here."""

    __slots__ = ("location",)

    def __init__(self, location: str | None) -> None:
        self.location = location


async def _ssdp_msearch(target: str, *, timeout: float) -> list[_SsdpResponse]:
    """
    Send an SSDP M-SEARCH and collect responses for ``timeout`` seconds.

    Patched in tests.
    """
    loop = asyncio.get_event_loop()
    transport: asyncio.DatagramTransport | None = None
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _SsdpProtocol,
            family=socket.AF_INET,
        )
        message = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_GROUP[0]}:{SSDP_GROUP[1]}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"ST: {target}\r\n"
            "MX: 3\r\n"
            "\r\n"
        )
        transport.sendto(message.encode("ascii"), SSDP_GROUP)
        await asyncio.sleep(timeout)
        return [_SsdpResponse(loc) for loc in protocol.locations]
    finally:
        if transport is not None:
            transport.close()


class _SsdpProtocol(asyncio.DatagramProtocol):
    """Collects ``LOCATION`` headers from M-SEARCH responses."""

    def __init__(self) -> None:
        self.locations: list[str] = []

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        for line in data.decode(errors="replace").splitlines():
            if line.lower().startswith("location:"):
                self.locations.append(line.split(":", 1)[1].strip())
                return


async def _fetch_dial_xml(
    session: aiohttp.ClientSession, location: str, *, timeout: float
) -> bytes:
    """Fetch the DIAL device description XML. Patched in tests."""
    async with session.get(
        location, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as resp:
        return await resp.read()


def _ssdp_to_device(location: str, xml: bytes) -> DiscoveredDevice | None:
    """Parse a DIAL description XML; return ``None`` if not a Vizio device."""
    try:
        root = ElementTree.fromstring(xml)
    except ParseError:
        return None

    device_el = _find_first(root, "device")
    if device_el is None:
        return None

    manufacturer = _find_text(device_el, "manufacturer")
    if manufacturer != "VIZIO":
        return None

    name = _find_text(device_el, "friendlyName") or "Vizio device"
    model = _find_text(device_el, "modelName") or ""
    udn = _find_text(device_el, "UDN") or ""

    ip, port = _split_location(location)
    if ip is None:
        return None

    return DiscoveredDevice(
        name=name,
        ip=ip,
        port=port,
        model=model,
        id=udn,
    )


def _split_location(location: str) -> tuple[str | None, int]:
    """``http://192.168.1.50:8008/...`` → ``("192.168.1.50", 8008)``."""
    parts = urlsplit(location)
    return parts.hostname, parts.port or 0


def _find_first(root: ElementTree.Element, tag: str) -> ElementTree.Element | None:
    """
    Find the first child element with local name ``tag``, ignoring XML namespaces.

    DIAL XML uses the upnp-org namespace and we don't care about the
    prefix on element names.
    """
    for el in root.iter():
        if _local_name(el.tag) == tag:
            return el
    return None


def _find_text(parent: ElementTree.Element, tag: str) -> str:
    el = _find_first(parent, tag)
    return (el.text or "").strip() if el is not None else ""


def _local_name(tag: str) -> str:
    """``{namespace}localname`` → ``localname``."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


# ---------------------------------------------------------------------------
# Unified discover
# ---------------------------------------------------------------------------


async def discover(
    timeout: float = DEFAULT_TIMEOUT,
    *,
    include_ssdp: bool = True,
) -> list[DiscoveredDevice]:
    """
    Discover Vizio devices on the local network.

    Runs zeroconf + SSDP concurrently. Dedupes by IP, preferring zeroconf
    metadata when both protocols find the same device.

    If the ``[discovery]`` extra is not installed, zeroconf raises
    :class:`ImportError`; we catch and log it, then fall back to SSDP
    only (so partial discovery is still useful).
    """

    async def _safe_zeroconf() -> list[DiscoveredDevice]:
        try:
            return await discover_zeroconf(timeout=timeout)
        except ImportError as e:
            _LOGGER.debug("Zeroconf discovery skipped: %s", e)
            return []

    async def _safe_ssdp() -> list[DiscoveredDevice]:
        if not include_ssdp:
            return []
        try:
            return await discover_ssdp(timeout=timeout)
        except Exception as e:
            _LOGGER.debug("SSDP discovery failed: %s", e)
            return []

    zc_results, ssdp_results = await asyncio.gather(_safe_zeroconf(), _safe_ssdp())
    return _merge_by_ip(zc_results, ssdp_results)


def _merge_by_ip(
    primary: Iterable[DiscoveredDevice],
    secondary: Iterable[DiscoveredDevice],
) -> list[DiscoveredDevice]:
    """Merge two device lists, preferring ``primary`` for duplicate IPs."""
    seen_ips: dict[str, DiscoveredDevice] = {}
    for d in primary:
        seen_ips.setdefault(d.ip, d)
    for d in secondary:
        seen_ips.setdefault(d.ip, d)
    return list(seen_ips.values())


# ---------------------------------------------------------------------------
# Host probe — pre-construction TV-vs-audio detection
# ---------------------------------------------------------------------------


async def async_is_tv(
    host: str,
    *,
    port: int | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float | None = None,
) -> bool:
    """
    Probe ``host`` to determine whether it's a Vizio TV (vs. an audio device).

    Uses the asymmetry that audio devices serve their settings tree without
    auth while TVs require it: hits the audio-settings root with no auth
    token. Success → audio device → returns ``False``. Any failure
    (auth required, connection error, malformed response) → ``True``.

    Lenient like pyvizio's ``async_guess_device_type``: an unreachable or
    otherwise misbehaving device is classified as TV — the dominant case
    for HA users, and a wrong guess gets corrected when the config flow
    shows the user a confirmation form.

    ``host`` may be ``"1.2.3.4"`` or ``"1.2.3.4:7345"``. When ``port`` is
    given separately, ``host`` must not include one.
    """
    if port is not None:
        if ":" in host:
            raise ValueError(
                "host already includes a port; pass `port` only when host is bare"
            )
        host = f"{host}:{port}"
    async with Vizio(
        host,
        device_type=DeviceType.SOUNDBAR,
        session=session,
        timeout=timeout,
    ) as device:
        try:
            await device.ping_auth()
        except VizioError:
            return True
    return False


# ---------------------------------------------------------------------------
# Pure model-string classifiers (layers 2 + 3)
# ---------------------------------------------------------------------------


def is_crave_model(model: str) -> bool:
    """
    Return ``True`` iff ``model`` is a Crave-family identifier.

    Crave models follow the ``SP*-*`` pattern (``SP30-E0``,
    ``SP50-D5``, ``SP70-D5``); no other Vizio audio device uses this
    prefix. Case-insensitive. Empty string returns ``False``.
    """
    return model.upper().startswith("SP")


def classify_crave_model(model: str) -> DeviceType:
    """
    Map a Crave-family model string to its specific ``DeviceType`` variant.

    | Prefix (case-insensitive) | Returns                  |
    |---------------------------|--------------------------|
    | ``SP30*``                 | ``DeviceType.CRAVE_GO``  |
    | ``SP50*``                 | ``DeviceType.CRAVE360``  |
    | ``SP70*``                 | ``DeviceType.CRAVE_PRO`` |
    | other ``SP*``             | ``DeviceType.CRAVE_GO``  |

    **Precondition:** ``is_crave_model(model)`` must return ``True``.
    Raises :class:`ValueError` otherwise — calling this on a TV or
    soundbar model is a programmer error.
    """
    if not is_crave_model(model):
        raise ValueError(f"{model!r} is not a Crave model")
    upper = model.upper()
    if upper.startswith("SP30"):
        return DeviceType.CRAVE_GO
    if upper.startswith("SP50"):
        return DeviceType.CRAVE360
    if upper.startswith("SP70"):
        return DeviceType.CRAVE_PRO
    # Unknown SP* variant: default to lowest-spec.
    return DeviceType.CRAVE_GO


async def async_classify_device(
    host: str,
    *,
    port: int | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float | None = None,
) -> DeviceType:
    """
    Classify ``host`` into one of the five :class:`DeviceType` values.

    Walks the three classification layers:

    1. :func:`async_is_tv` — TV vs audio via auth-asymmetry probe.
    2. :func:`is_crave_model` — soundbar vs Crave family by
       ``MODEL_NAME`` prefix.
    3. :func:`classify_crave_model` — specific Crave variant.

    Lenient on failure: an unreachable host returns ``DeviceType.TV``
    (matching :func:`async_is_tv`); a reachable audio host whose
    deviceinfo fetch fails returns ``DeviceType.SOUNDBAR`` (we already
    established it's not a TV at step 1).

    ``host`` may include a port (``"1.2.3.4:7345"``) or accept one via
    the ``port`` kwarg — same shape as :func:`async_is_tv`.
    """
    if await async_is_tv(host, port=port, session=session, timeout=timeout):
        return DeviceType.TV
    if port is not None and ":" not in host:
        host = f"{host}:{port}"
    async with Vizio(
        host,
        device_type=DeviceType.SOUNDBAR,
        session=session,
        timeout=timeout,
    ) as device:
        try:
            response = await device._request(Endpoint.DEVICE_INFO)
        except VizioError:
            return DeviceType.SOUNDBAR
        model = parse_system_info_model_name(response)
    if is_crave_model(model):
        return classify_crave_model(model)
    return DeviceType.SOUNDBAR
