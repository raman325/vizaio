"""WebSocket SCPL — event subscription tests.

Strategy:
- ``_parse_event_frame`` is pure JSON-in / event-out — exercised
  directly with synthetic frames mirroring the inferred shape from
  ``docs/websocket-protocol-notes.md``.
- ``EventStream`` is exercised by mocking aiohttp's ``ws_connect`` and
  the underlying ``SmartCastClient.request_spec`` (for the register
  call). We feed canned frames through a fake WebSocket to verify the
  consumer's iteration semantics.

What the tests intentionally *don't* prove:
- That the inferred event-payload shape (URI + STATUS + ITEMS) matches
  what a real Vizio TV actually emits. The agent flagged this as
  needing hardware verification — see protocol-notes #28 and the
  ``# HW-VERIFY`` markers below.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from vizio_smartcast import (
    DeviceType,
    StateEvent,
    SubscribeOptions,
    Vizio,
    VizioConnectionError,
)
from vizio_smartcast._websocket import (
    EVENT_REGISTER_BODY,
    EVENT_REGISTER_PATH,
    KNOWN_URIS,
    _parse_event_frame,
)

TV_HOST = "192.168.1.50:7345"
TOKEN = "auth-fixture"


# ===========================================================================
# Pure event-frame parsing — no transport
# ===========================================================================


def _make_frame(
    uri: str,
    *,
    value: Any = None,
    cname: str | None = None,
    hashval: int | None = None,
    items: list[dict[str, Any]] | None = None,
    status: str = "SUCCESS",
) -> str:
    """Synthesize an inferred-shape frame.

    HW-VERIFY: this is the assumption surface. Real device frames may
    differ; once we have hardware, capture and replace.
    """
    if items is None and value is not None:
        items = [
            {
                "CNAME": cname or uri.rsplit("/", 1)[-1],
                "VALUE": value,
                "HASHVAL": hashval if hashval is not None else 0,
            }
        ]
    body: dict[str, Any] = {
        "URI": uri,
        "STATUS": {"RESULT": status, "DETAIL": ""},
    }
    if items is not None:
        body["ITEMS"] = items
    return json.dumps(body)


class TestParseEventFrame:
    """Tolerant parsing of WS text frames into StateEvent."""

    def test_full_envelope(self) -> None:
        text = _make_frame("audio/volume/level", value=25, cname="volume", hashval=99)
        event = _parse_event_frame(text)
        assert event is not None
        assert event.uri == "audio/volume/level"
        assert event.value == 25
        assert event.cname == "volume"
        assert event.hashval == 99

    def test_lowercases_envelope_keys(self) -> None:
        """Wire-boundary discipline: caller-facing fields are lowercase
        regardless of the device's casing of envelope keys."""
        text = _make_frame("state/device/power_mode", value=1, cname="power_mode")
        event = _parse_event_frame(text)
        assert event is not None
        assert "uri" in event.raw  # lowercased
        assert event.cname == "power_mode"

    def test_missing_uri_returns_none(self) -> None:
        """Frames without URI are unparseable — drop, don't crash."""
        assert _parse_event_frame('{"STATUS": {"RESULT": "SUCCESS"}}') is None

    def test_malformed_json_returns_none(self) -> None:
        assert _parse_event_frame("not json") is None

    def test_non_object_returns_none(self) -> None:
        assert _parse_event_frame("[1, 2, 3]") is None
        assert _parse_event_frame('"just a string"') is None

    def test_uri_without_items(self) -> None:
        """Some frames may have just URI (e.g., the inferred-shape
        assumption may be wrong). Surface the URI; value/cname empty."""
        event = _parse_event_frame('{"URI": "system/context_change"}')
        assert event is not None
        assert event.uri == "system/context_change"
        assert event.value is None
        assert event.cname == ""
        assert event.hashval is None

    def test_empty_items(self) -> None:
        text = json.dumps({"URI": "foo", "ITEMS": []})
        event = _parse_event_frame(text)
        assert event is not None
        assert event.value is None

    def test_items_not_a_list(self) -> None:
        """Defensive: device ITEMS as a dict instead of list shouldn't crash."""
        text = json.dumps({"URI": "foo", "ITEMS": {"CNAME": "x"}})
        event = _parse_event_frame(text)
        assert event is not None
        assert event.value is None  # we only know how to read list-shaped ITEMS

    def test_string_value(self) -> None:
        """Mute is reported as 'On'/'Off' in REST responses; assume the
        same for events."""
        text = _make_frame("audio/volume/mute", value="On", cname="mute")
        event = _parse_event_frame(text)
        assert event is not None
        assert event.value == "On"

    def test_dict_value_preserved(self) -> None:
        """Apps return VALUE as a nested dict in REST. Same likely here."""
        text = _make_frame(
            "app/current",
            value=None,
            items=[{"CNAME": "current_app", "VALUE": {"APP_ID": "3", "NAME_SPACE": 2}}],
        )
        event = _parse_event_frame(text)
        assert event is not None
        assert isinstance(event.value, dict)

    def test_hashval_int_coercion(self) -> None:
        """Hashval may arrive as a string in some firmware; coerce."""
        text = json.dumps(
            {
                "URI": "audio/volume/level",
                "ITEMS": [{"CNAME": "volume", "VALUE": 25, "HASHVAL": "12345"}],
            }
        )
        event = _parse_event_frame(text)
        assert event is not None
        assert event.hashval == 12345


class TestKnownUris:
    """Sanity: the documented set matches the APK demultiplexer list."""

    def test_five_known_uris(self) -> None:
        assert (
            frozenset(
                {
                    "state/device/power_mode",
                    "app/current",
                    "system/context_change",
                    "audio/volume/level",
                    "audio/volume/mute",
                }
            )
            == KNOWN_URIS
        )


# ===========================================================================
# EventStream — connection lifecycle, register-then-upgrade, iteration
# ===========================================================================


class _FakeWS:
    """Stand-in for ``aiohttp.ClientWebSocketResponse``.

    Iterates a queue of frames (text strings) then closes. We don't
    drive a real WS — just verify our consumer pulls and parses
    correctly.
    """

    def __init__(self, frames: list[Any]) -> None:
        self._frames = list(frames)
        self.closed = False

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        if not self._frames:
            self.closed = True
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def close(self) -> None:
        self.closed = True

    def exception(self) -> Exception | None:
        return None


def _text_msg(text: str) -> Any:
    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.TEXT
    msg.data = text
    return msg


def _close_msg() -> Any:
    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.CLOSE
    return msg


def _binary_msg(data: bytes) -> Any:
    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.BINARY
    msg.data = data
    return msg


def _error_msg() -> Any:
    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.ERROR
    return msg


@pytest.fixture
def vizio_tv() -> Vizio:
    return Vizio(host=TV_HOST, device_type=DeviceType.TV, auth_token=TOKEN)


@pytest.fixture
def mock_register(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace SmartCastClient.request_spec — used for /event/register."""
    from vizio_smartcast.wire import Response

    mock = AsyncMock(
        return_value=Response.from_json({"STATUS": {"RESULT": "SUCCESS", "DETAIL": ""}})
    )
    monkeypatch.setattr("vizio_smartcast.client.SmartCastClient.request_spec", mock)
    return mock


@pytest.fixture
def mock_ws_connect(monkeypatch: pytest.MonkeyPatch):
    """Replace aiohttp.ClientSession.ws_connect with a settable factory."""
    factory = MagicMock()
    fake_ws = _FakeWS([])
    factory.return_value = fake_ws

    async def _aw(*args: Any, **kwargs: Any) -> _FakeWS:
        return factory(*args, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", _aw)
    return factory, fake_ws


class TestEventStreamConnect:
    async def test_calls_register_first(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        factory, fake_ws = mock_ws_connect
        fake_ws._frames = [_close_msg()]
        async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
            async for _ in events:
                pass
        # Register was the first call.
        spec = mock_register.call_args_list[0].args[0]
        assert spec.paths == (EVENT_REGISTER_PATH,)
        assert spec.method == "PUT"
        body = mock_register.call_args_list[0].kwargs.get("body")
        assert body == EVENT_REGISTER_BODY

    async def test_ws_url_carries_token_query(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        factory, fake_ws = mock_ws_connect
        fake_ws._frames = [_close_msg()]
        async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
            async for _ in events:
                pass
        # Method-level monkeypatch: args[0] is the ClientSession (self),
        # args[1] is the URL.
        url = factory.call_args.args[1]
        assert url.startswith("wss://192.168.1.50:7345/?TOKEN=")
        assert TOKEN in url

    async def test_ws_headers_use_authorization(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        """Note: REST uses ``AUTH``, WS uses ``Authorization``. Confirmed APK."""
        factory, fake_ws = mock_ws_connect
        fake_ws._frames = [_close_msg()]
        async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
            async for _ in events:
                pass
        headers = factory.call_args.kwargs["headers"]
        assert headers["Authorization"] == TOKEN
        assert headers["VIZIO-SmartCast-Source"] == "vizio-smartcast"

    async def test_ws_port_override(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        factory, fake_ws = mock_ws_connect
        fake_ws._frames = [_close_msg()]
        async with vizio_tv.subscribe_events(
            ws_port=9999, auto_reconnect=False
        ) as events:
            async for _ in events:
                pass
        assert ":9999/" in factory.call_args.args[1]

    async def test_register_failure_propagates(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        mock_register.side_effect = VizioConnectionError("register refused")
        with pytest.raises(VizioConnectionError, match="register refused"):
            async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
                async for _ in events:
                    pass

    async def test_invalid_parameter_remaps_to_unsupported(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        """
        Captured live from VHD24M-0810 fw 3.720.9.1-1: PUT /event/register
        returns ``STATUS.RESULT="INVALID_PARAMETER"`` on devices that
        don't support push events (Marvell-SoC TVs, audio-only devices,
        older firmware). The bare InvalidParameter error is misleading
        — it sounds like a request shape problem when in fact the
        request was correctly formed and the *device* doesn't support
        the feature. Map it to VizioUnsupportedError so callers can
        catch it as the capability check it actually is.
        """
        from vizio_smartcast.errors import (
            VizioInvalidParameterError,
            VizioUnsupportedError,
        )

        mock_register.side_effect = VizioInvalidParameterError("Invalid parameter")
        with pytest.raises(VizioUnsupportedError, match="push events"):
            async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
                async for _ in events:
                    pass


class TestEventStreamIteration:
    async def test_yields_parsed_events(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        _, fake_ws = mock_ws_connect
        fake_ws._frames = [
            _text_msg(_make_frame("audio/volume/level", value=15, cname="volume")),
            _text_msg(_make_frame("audio/volume/mute", value="On", cname="mute")),
            _close_msg(),
        ]
        seen: list[StateEvent] = []
        async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
            async for event in events:
                seen.append(event)
        assert len(seen) == 2
        assert seen[0].uri == "audio/volume/level"
        assert seen[0].value == 15
        assert seen[1].value == "On"

    async def test_skips_malformed_frames(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        _, fake_ws = mock_ws_connect
        fake_ws._frames = [
            _text_msg("not json"),
            _text_msg('{"STATUS": "no uri"}'),  # missing URI
            _text_msg(
                _make_frame("state/device/power_mode", value=1, cname="power_mode")
            ),
            _close_msg(),
        ]
        seen: list[StateEvent] = []
        async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
            async for event in events:
                seen.append(event)
        assert len(seen) == 1
        assert seen[0].uri == "state/device/power_mode"

    async def test_ignores_binary_frames(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        """Voice-streaming reuses the same WS port with binary frames in
        the official app. We don't handle voice — just skip silently."""
        _, fake_ws = mock_ws_connect
        fake_ws._frames = [
            _binary_msg(b"audio data"),
            _text_msg(_make_frame("audio/volume/level", value=42, cname="volume")),
            _close_msg(),
        ]
        seen: list[StateEvent] = []
        async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
            async for event in events:
                seen.append(event)
        assert len(seen) == 1
        assert seen[0].value == 42

    async def test_close_frame_ends_iteration(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        _, fake_ws = mock_ws_connect
        fake_ws._frames = [_close_msg()]
        seen: list[StateEvent] = []
        async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
            async for event in events:
                seen.append(event)
        assert seen == []

    async def test_no_auto_reconnect_ends_after_close(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        _, fake_ws = mock_ws_connect
        fake_ws._frames = [
            _text_msg(_make_frame("audio/volume/level", value=1, cname="volume")),
            _close_msg(),
        ]
        async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
            count = 0
            async for _ in events:
                count += 1
        assert count == 1
        # Register called exactly once — no reconnect.
        register_calls = [
            c
            for c in mock_register.call_args_list
            if c.args[0].paths == (EVENT_REGISTER_PATH,)
        ]
        assert len(register_calls) == 1


class TestEventStreamReconnect:
    """Reconnect-after-disconnect behavior. Each cycle re-registers."""

    async def test_reconnects_with_zero_delay(
        self,
        monkeypatch: pytest.MonkeyPatch,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
    ) -> None:
        """Two WS sessions back-to-back: stream ends only when caller
        breaks out, not when one session closes."""

        # Build two fake WS instances served in order.
        first_frame = _make_frame("audio/volume/level", value=1, cname="volume")
        second_frame = _make_frame("audio/volume/level", value=2, cname="volume")
        sessions = [
            _FakeWS([_text_msg(first_frame), _close_msg()]),
            _FakeWS([_text_msg(second_frame), _close_msg()]),
        ]
        order = iter(sessions)

        async def _ws_connect(*args: Any, **kwargs: Any) -> _FakeWS:
            return next(order)

        monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", _ws_connect)

        seen: list[int] = []
        async with vizio_tv.subscribe_events(reconnect_delay=0.0) as events:
            async for event in events:
                seen.append(event.value)
                if len(seen) == 2:
                    break

        assert seen == [1, 2]
        # Register was called for each cycle.
        register_calls = [
            c
            for c in mock_register.call_args_list
            if c.args[0].paths == (EVENT_REGISTER_PATH,)
        ]
        assert len(register_calls) == 2


class TestEventStreamErrors:
    async def test_transport_error_after_first_event_reconnects(
        self,
        monkeypatch: pytest.MonkeyPatch,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
    ) -> None:
        """An ERROR frame mid-stream raises VizioConnectionError; the
        outer loop catches and reconnects (with a small delay)."""
        good = _FakeWS(
            [
                _text_msg(_make_frame("audio/volume/level", value=10, cname="volume")),
                _error_msg(),
            ]
        )
        recovered = _FakeWS(
            [
                _text_msg(_make_frame("audio/volume/level", value=20, cname="volume")),
                _close_msg(),
            ]
        )
        order = iter([good, recovered])

        async def _ws_connect(*args: Any, **kwargs: Any) -> _FakeWS:
            return next(order)

        monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", _ws_connect)

        seen: list[int] = []
        async with vizio_tv.subscribe_events(reconnect_delay=0.0) as events:
            async for event in events:
                seen.append(event.value)
                if len(seen) == 2:
                    break
        assert seen == [10, 20]

    async def test_first_connect_failure_does_not_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
    ) -> None:
        """If the very first ``ws_connect`` raises, surface immediately —
        don't silently wait reconnect_delay seconds."""

        async def _bad(*args: Any, **kwargs: Any) -> Any:
            raise aiohttp.ClientConnectionError("refused")

        monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", _bad)

        with pytest.raises(VizioConnectionError):
            async with vizio_tv.subscribe_events() as events:
                async for _ in events:
                    pass


class TestEventStreamLifecycle:
    async def test_aclose_idempotent(
        self,
        vizio_tv: Vizio,
        mock_register: AsyncMock,
        mock_ws_connect: tuple[MagicMock, _FakeWS],
    ) -> None:
        _, fake_ws = mock_ws_connect
        fake_ws._frames = [_close_msg()]
        async with vizio_tv.subscribe_events(auto_reconnect=False) as events:
            await events.aclose()
            await events.aclose()  # second close: no error

    async def test_options_immutable(self) -> None:
        opts = SubscribeOptions(ws_port=1234)
        with pytest.raises((AttributeError, TypeError)):
            opts.ws_port = 5678  # type: ignore[misc]
