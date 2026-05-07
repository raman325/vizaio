"""
WebSocket SCPL — event subscription.

Implementation of the protocol documented in
``docs/websocket-protocol-notes.md`` (APK-derived, hardware-verification
pending). Used by :meth:`Vizio.subscribe_events`.

Public:

- :class:`EventStream` — async iterator of :class:`StateEvent` over a
  live WebSocket connection.
- :class:`SubscribeOptions` — knobs for reconnect behavior, port choice.

Protocol summary (APK-confirmed unless noted):

1. Send ``PUT /event/register`` over the regular HTTPS REST agent with
   body ``{"REQUEST": "MODIFY"}`` and the ``AUTH`` header. This is a
   global "send me events" toggle — there is no per-cname subscription.
2. On success, open ``wss://{host}:{ws_port}/?TOKEN={auth_token}`` with
   the ``Authorization: <token>`` header (note: capital-A, unlike the
   REST ``AUTH`` header).
3. Receive ``TextWebSocketFrame`` JSON envelopes. Each has at minimum a
   ``URI`` field; we infer the rest of the envelope mirrors REST
   responses (``STATUS``/``ITEMS``). Hardware verification will confirm.
4. Heartbeat: client sends a WS ping after ~3s of write-idle; client
   closes after ~10s of read-idle.
5. Disconnect: re-register and re-open. The TV does not remember
   subscriptions across reconnects.

Known TV-only path: the Android app gates this on ``device_type ==
TV`` and excludes Marvell-SoC TVs. We don't enforce that — we probe.
If ``PUT /event/register`` succeeds, we proceed; if it doesn't, the
caller's ``async with`` context propagates the error.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING, Any, Self

import aiohttp

from .endpoints import EndpointSpec
from .errors import (
    VizioAuthError,
    VizioConnectionError,
    VizioError,
    VizioInvalidParameterError,
    VizioResponseError,
    VizioUnsupportedError,
)
from .types import AuthRequirement, StateEvent
from .wire import _lowercase_keys

if TYPE_CHECKING:
    from .client import SmartCastClient

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — see docs/websocket-protocol-notes.md
# ---------------------------------------------------------------------------

EVENT_REGISTER_PATH = "/event/register"
"""HTTP endpoint that toggles event broadcasting for an auth token."""

EVENT_REGISTER_BODY = {"REQUEST": "MODIFY", "VALUE": "TRUE"}
"""The body sent on ``PUT /event/register``.

The APK reverse-engineering surfaced ``{"REQUEST": "MODIFY"}`` alone,
but live testing against a VHD24M-0810 (firmware 3.720.9.1-1) showed
that body returns ``INVALID_PARAMETER``; adding ``VALUE: "TRUE"``
makes the same device return ``SUCCESS``. The string-typed ``"TRUE"``
matters: ``true`` (JSON bool) crashes the device's parser and returns
an HTML error page. Newer firmware appears to require the ``VALUE``
field even though the APK's ``Body`` model serializes it as null."""

WS_DEFAULT_PORT = 7345
"""Fallback when the device hasn't advertised a wsPort/wssPort via mDNS."""

# Per APK ``EmbeddedConnectionConfig`` defaults.
WS_PING_INTERVAL = 3.0
WS_READ_TIMEOUT = 10.0
WS_REGISTER_TIMEOUT = 3.0

# Per APK ``DeviceWebsocketMonitor.DELAY_SETUP_RETRY_MS``.
WS_RECONNECT_DELAY = 15.0

KNOWN_URIS = frozenset(
    {
        "state/device/power_mode",
        "app/current",
        "system/context_change",
        "audio/volume/level",
        "audio/volume/mute",
    }
)
"""URIs the official Android app explicitly demultiplexes. The TV may
emit others; the app silently ignores them. We surface everything."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubscribeOptions:
    """Knobs for :class:`EventStream`."""

    ws_port: int | None = None
    """Override the WebSocket port. ``None`` → use the host's port if
    present, else :data:`WS_DEFAULT_PORT`."""

    auto_reconnect: bool = True
    """When ``True``, ``async for`` keeps yielding across drops by
    re-registering and re-opening. When ``False``, the iterator ends on
    first disconnect."""

    reconnect_delay: float = WS_RECONNECT_DELAY
    """Seconds between reconnect attempts."""

    ping_interval: float = WS_PING_INTERVAL
    receive_timeout: float = WS_READ_TIMEOUT


class EventStream:
    """
    Async context manager + iterator yielding :class:`StateEvent` records.

    Typical usage::

        async with vizio.subscribe_events() as events:
            async for event in events:
                if event.uri == "audio/volume/level":
                    self.update_volume(event.value)

    Lifecycle:

    - ``__aenter__`` does nothing — the connection is established lazily
      on the first iteration. This avoids the awkward case where the
      caller wants to construct a stream but defer connect work.
    - The iterator pulls one frame at a time, parsing it into a
      ``StateEvent``. Disconnects → reconnect (per options) → continue.
    - ``__aexit__`` closes the underlying WebSocket cleanly.

    The class is **not** safe to share across tasks — one consumer per
    stream. Construct multiple streams if you need fan-out.
    """

    def __init__(
        self,
        *,
        client: SmartCastClient,
        host: str,
        auth_token: str | None,
        options: SubscribeOptions,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Stash params; the WebSocket open is deferred until iteration."""
        self._client = client
        self._host = host
        self._auth_token = auth_token
        self._options = options
        self._session = session
        self._owns_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Async-context-manager + async-iterator protocols
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        """Enter the context manager (no-op; connection is lazy)."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Close the WebSocket and the owned aiohttp session, if any."""
        await self.aclose()

    def __aiter__(self) -> AsyncIterator[StateEvent]:
        """Return the async iterator over reconnecting :class:`StateEvent`s."""
        return self._iterate()

    async def aclose(self) -> None:
        """Close the WebSocket and the owned aiohttp session, if any."""
        if self._closed:
            return
        self._closed = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._owns_session and self._session is not None:
            await self._session.close()

    # ------------------------------------------------------------------
    # Internal — connection establishment + iteration
    # ------------------------------------------------------------------

    async def _iterate(self) -> AsyncIterator[StateEvent]:
        """Yield :class:`StateEvent`s; reconnect on drops, stop on auth fail."""
        first_attempt = True
        while not self._closed:
            try:
                if first_attempt or self._ws is None or self._ws.closed:
                    await self._connect()
                first_attempt = False
                async for event in self._consume_frames():
                    yield event
            except VizioError:
                # Surface the first connect failure to the caller; once
                # we've yielded at least one event the loop keeps going.
                if first_attempt:
                    raise
                _LOGGER.debug("WebSocket session ended; reconnecting")
            except aiohttp.ClientError as e:
                if first_attempt:
                    raise VizioConnectionError(
                        f"WebSocket connection failed: {e}"
                    ) from e
                _LOGGER.debug("WebSocket transport error: %s", e)

            if self._closed or not self._options.auto_reconnect:
                return

            await asyncio.sleep(self._options.reconnect_delay)

    async def _connect(self) -> None:
        """Run ``register → upgrade``, leaving ``self._ws`` ready to read."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
            self._ws = None

        await self._register_for_events()

        session = await self._ensure_session()
        url = self._build_ws_url()
        headers = self._build_ws_headers()
        try:
            self._ws = await asyncio.wait_for(
                session.ws_connect(
                    url,
                    headers=headers,
                    heartbeat=self._options.ping_interval,
                    receive_timeout=self._options.receive_timeout,
                    ssl=False,
                ),
                timeout=WS_REGISTER_TIMEOUT,
            )
        except TimeoutError as e:
            raise VizioConnectionError(
                f"WebSocket upgrade timed out connecting to {url}"
            ) from e

    async def _register_for_events(self) -> None:
        """
        Send ``PUT /event/register`` over the existing REST client.

        We reuse :class:`SmartCastClient` rather than building a parallel
        HTTP path — same auth header, same SSL config, same semaphore.

        Per APK reverse-engineering, the device gates WebSocket event
        support by accepting or rejecting this register call. The
        Android app excludes Marvell-SoC TVs and audio-only devices
        from the WS pipeline (it never even attempts the register on
        those). When the library probes against an unsupported device,
        we get :class:`VizioInvalidParameterError` here. Verified live
        on VHD24M-0810 fw 3.720.9.1-1, which is a Marvell-class
        chipset. Re-raise as :class:`VizioUnsupportedError` so callers
        can distinguish "device doesn't support push events; fall back
        to polling" from a generic protocol error.
        """
        spec = EndpointSpec(
            paths=(EVENT_REGISTER_PATH,),
            method="PUT",
            auth=(
                AuthRequirement.REQUIRED
                if self._auth_token
                else AuthRequirement.OPTIONAL
            ),
            item_cname=None,
        )
        try:
            await self._client.request_spec(spec, body=EVENT_REGISTER_BODY)
        except VizioInvalidParameterError as e:
            raise VizioUnsupportedError(
                "device rejected event-register — push events are not "
                "supported on this firmware/chipset; use polling instead"
            ) from e

    async def _consume_frames(self) -> AsyncIterator[StateEvent]:
        """
        Yield one event per inbound text frame.

        Returns when the socket closes — the outer loop decides whether
        to reconnect.
        """
        if self._ws is None:  # pragma: no cover — guarded by _connect
            return
        async for msg in self._ws:
            if msg.type is aiohttp.WSMsgType.TEXT:
                event = _parse_event_frame(msg.data)
                if event is not None:
                    yield event
            elif msg.type is aiohttp.WSMsgType.BINARY:
                # Voice-streaming side-channel reuses this port in the
                # official app. We don't handle it.
                _LOGGER.debug("ignoring binary WS frame (%d bytes)", len(msg.data))
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            ):
                _LOGGER.debug("WebSocket closed cleanly")
                return
            elif msg.type is aiohttp.WSMsgType.ERROR:
                exc = self._ws.exception()
                raise VizioConnectionError(f"WebSocket transport error: {exc}")

    # ------------------------------------------------------------------
    # URL / headers / session
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Lazily create the owned aiohttp session on first connect."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
            )
        return self._session

    def _build_ws_url(self) -> str:
        """Build the ``wss://host:port/?TOKEN=…`` URL for the WebSocket upgrade."""
        host = self._strip_port(self._host)
        port = self._options.ws_port or self._port_from_host() or WS_DEFAULT_PORT
        suffix = f"?TOKEN={self._auth_token}" if self._auth_token else ""
        return f"wss://{host}:{port}/{suffix}"

    def _build_ws_headers(self) -> dict[str, str]:
        """Build WS upgrade headers (WS uses ``Authorization``, not REST's ``AUTH``)."""
        headers = {"VIZIO-SmartCast-Source": "vizaio"}
        if self._auth_token:
            # Note: WS upgrade uses ``Authorization`` (capital-A,
            # full word). REST uses ``AUTH``. APK confirms.
            headers["Authorization"] = self._auth_token
        return headers

    def _strip_port(self, host: str) -> str:
        """Return ``host`` with any ``:port`` suffix removed."""
        return host.split(":", 1)[0] if ":" in host else host

    def _port_from_host(self) -> int | None:
        """Extract the port number from ``self._host`` if present, else ``None``."""
        if ":" not in self._host:
            return None
        try:
            return int(self._host.rsplit(":", 1)[1])
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Frame parsing — the assumption surface that may need adjustment after
# hardware verification (FLAGGED in protocol notes #28).
# ---------------------------------------------------------------------------


def _parse_event_frame(text: str) -> StateEvent | None:
    """
    Decode one text frame into a :class:`StateEvent`.

    Returns ``None`` for frames we can't interpret at all (malformed
    JSON, missing URI). The caller's iterator continues.

    Inferred shape — see docs/websocket-protocol-notes.md.
    """
    try:
        raw = json.loads(text)
    except (TypeError, ValueError) as e:
        _LOGGER.debug("ignoring non-JSON WS frame: %s", e)
        return None

    if not isinstance(raw, Mapping):
        _LOGGER.debug("ignoring non-object WS frame: %r", type(raw).__name__)
        return None

    normalized = _lowercase_keys(raw)
    uri = normalized.get("uri")
    if not isinstance(uri, str):
        _LOGGER.debug("ignoring WS frame without URI")
        return None

    value, cname, hashval = _extract_item(normalized)
    return StateEvent(
        uri=uri.strip(),
        value=value,
        cname=cname,
        hashval=hashval,
        raw=normalized,
    )


def _extract_item(
    normalized: Mapping[str, Any],
) -> tuple[Any, str, int | None]:
    """
    Pull (value, cname, hashval) from the inferred ITEMS[0] shape.

    Tolerant: missing items, missing fields, or unexpected ITEMS shape
    all degrade to ``(None, "", None)`` rather than raising. The caller
    decides whether the missing data is a problem.
    """
    items = normalized.get("items")
    if not isinstance(items, list) or not items:
        return None, "", None
    first = items[0]
    if not isinstance(first, Mapping):
        return None, "", None
    cname = str(first.get("cname", "")).lower() if first.get("cname") else ""
    value = first.get("value")
    hashval_raw = first.get("hashval")
    try:
        hashval = int(hashval_raw) if hashval_raw is not None else None
    except (TypeError, ValueError):
        hashval = None
    return value, cname, hashval


# Re-exports useful for advanced callers / tests.
__all__ = [
    "EVENT_REGISTER_BODY",
    "EVENT_REGISTER_PATH",
    "KNOWN_URIS",
    "WS_DEFAULT_PORT",
    "WS_RECONNECT_DELAY",
    "EventStream",
    "SubscribeOptions",
    "_parse_event_frame",
]


# ---------------------------------------------------------------------------
# Internal helper: VizioAuthError import dance to avoid circular imports
# ---------------------------------------------------------------------------

# (Used implicitly via VizioError catching above; kept here for clarity.)
_ = VizioAuthError, VizioResponseError
