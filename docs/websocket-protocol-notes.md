# Why vizaio has no WebSocket / push-event support

**Decision:** vizaio is a REST control-plane client only. It does **not**
implement the SmartCast WebSocket / event-subscription protocol. An earlier
prototype (`_websocket.py`, `subscribe_events()`, `StateEvent`) was removed
after hardware testing — see git history (`refactor/drop-websocket-events`).

## What we found (5.0.0 APK decompile + live TV)

The SmartCast "event" WebSocket is part of the device's **Google Cast control
plane**, not a general mDNS/REST feature:

- The official Android app's mDNS path (`DnsVizioParser.parse`) builds **only
  `https` connection capabilities**. It parses the TXT `wp`/`wsp`
  (wsPort/wssPort) keys but **never uses them to open a socket** — they are
  vestigial in this build.
- The **only** WebSocket the app ever opens is `ws://<ip>:8005` (insecure),
  hardcoded, and built **solely for Google-Cast-discovered devices**
  (`CastMediaRouterCallback.java:109`). An mDNS-discovered TV gets no WS agent,
  so `WebsocketConnectionStrategy.isReadyForWebsocket()` is false and the app
  **polls REST** instead.
- On a real TV (VHD24M-0810, fw 3.720.9.1-1, SoC **MTK** — not Marvell), the
  advertised `wp=8005`/`wsp=8006` ports are **connection-refused even when
  powered on**, `PUT /event/register` returns `SUCCESS` but no socket appears,
  and a WS upgrade to the REST port (7345, plain `lighttpd`) returns HTTP 500.
  Register success does **not** imply a reachable socket.

What the WS carries when it *is* up (Cast context): TV→client push for five
URIs (`state/device/power_mode`, `audio/volume/level`, `audio/volume/mute`,
`system/context_change`, `app/current`) and a client→TV voice-search audio
upload. Both are Cast/voice concerns, out of scope for a SmartCast REST client.

## Implication for users wanting push / real-time state

Discover the TV through its **Chromecast-built-in** interface — e.g. Home
Assistant's Google Cast integration auto-discovers it via `_googlecast._tcp`,
independent of vizaio — and poll vizaio for SmartCast-specific state
(inputs, settings, power, current app).
