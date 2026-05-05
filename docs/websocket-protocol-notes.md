# WebSocket SCPL Protocol Notes

Source: jadx decompile of `com.vizio.vue.launcher` 5.0.0 at
`/tmp/vizio-apk-analysis/jadx-newer/sources/`. All paths below are relative
to that root unless noted.

## TL;DR for `Vizio.subscribe_events()`

The big surprise: **`eventRegister` is not a WebSocket frame.** It is an HTTP
`PUT /event/register` call sent over the *HTTP* agent on port 7345/9000,
issued **before** the WebSocket is opened. After registration, the client
opens a `wss://host:<wsPort>/` connection and the TV pushes
`{"URI": "<cname>", ...}` JSON frames whenever a registered/system-watched
property changes. There is no per-cname subscription envelope sent on the
socket itself — it is one global "send me events" toggle plus a passive
listener.

## Connection

### URL & port — CONFIRMED

- Scheme: `wss` (preferred) or `ws`.
  `com/vizio/vnf/network/message/network/SchemesKt.java:11-12`:

  ```
  public static final String SCHEME_INSECURE_WEBSOCKET = "ws";
  public static final String SCHEME_SECURE_WEBSOCKET   = "wss";
  ```

- Port: from the device's mDNS TXT record, keys `wsPort` / `wssPort`.
  `com/vizio/vnf/network/agent/dns/decoder/DnsVizioTxtMessageParser.java:178,313`:

  ```
  mapWithDefaultMutable.put("wsPort",  …)   // line 178
  mapWithDefaultMutable.put("wssPort", …)   // line 313
  ```

  Default fallback if no port set: **443**
  (`WebSocketClient.java:237,242,402`). The Cast path also fabricates an
  insecure capability on **8005** (`CastMediaRouterCallback.java:109`),
  which appears to be a discovery hint, not the SCPL WS port.
- Path: `"/"` — hard-coded when the WS agent is built.
  `com/vizio/vdf/services/manager/DeviceApiManager.java:139`:

  ```
  EmbeddedConnectionConfig(authToken, capability.getScheme(),
      capability.getIpAddress(), capability.getPort(),
      null, "/", 0L, 0L, 0L, null, 976, null);
  ```

- Final URL assembly: `WebSocketClient.java:96`:

  ```
  uri = scheme + "://" + host + ":" + port + path
        + (authToken != null ? "?TOKEN=" + authToken : "")
  ```

  i.e. for an audio device with auth `Z9X8…` you'd get
  `wss://10.0.0.21:7345/?TOKEN=Z9X8…`.
  This is unusual — the auth token is also embedded as a query param,
  *in addition to* the `AUTH` header being sent on the upgrade (see below).

### Auth & headers — CONFIRMED

The handshake is set up in `WebSocketClient$channelPoolHandler$1.java:80-85`:

```
DefaultHttpHeaders defaultHttpHeaders = new DefaultHttpHeaders();
defaultHttpHeaders.add(HEADER_KEY_SMARTCAST_SOURCE, "SMARTCAST_ANDROID");
for (Map.Entry e : connectionConfig.getHeaders().entrySet())
    defaultHttpHeaders.add(e.getKey(), e.getValue());
WebSocketClientHandshakerFactory.newHandshaker(uri, V13, null, true,
                                               defaultHttpHeaders, 1048576);
```

- `VIZIO-SmartCast-Source: SMARTCAST_ANDROID` — always added.
  Constants in `com/vizio/vnf/network/message/SmartCastHeaders.java:11,15`.
- `Authorization: <authToken>` — added by `EmbeddedConnectionConfig`
  constructor (`EmbeddedConnectionConfig.java:33`) when `authToken != null`.
  Note this is `Authorization`, not the REST-style `AUTH` header.
- WebSocket version: **13** (RFC 6455 standard).
- No subprotocol, no `Origin`, no `User-Agent` set explicitly on the WS
  upgrade in this code path. `User-Agent` for REST is `"Ktor client"`
  (`SmartCastHeaders.KTOR_CLIENT`).
- `WebSocketClientCompressionHandler.INSTANCE` is in the pipeline, so
  `permessage-deflate` is offered.
  (`WebSocketClient$channelPoolHandler$1.java:130`).

### TLS — CONFIRMED self-signed-friendly

`InsecureTrustManagerFactory.INSTANCE` is the default
(`WebSocketClient.java:115`). Same as REST.

## Connection lifecycle

- Frame type: **text** for control + events, **binary** only for the
  voice/audio side-channel (handled by `WebsocketAudioStreamMessage`,
  see `WebSocketClient.java:153-166`). For the SCPL event channel you only
  ever receive `TextWebSocketFrame`.
  (`WebSocketClientHandler.java:118-126`).
- Heartbeat: client-driven ping. `IdleStateHandler` is wired with
  `(readTimeoutMs, writeTimeoutMs, 0)`; on `WRITER_IDLE` the client sends
  an empty `PingWebSocketFrame`; on `READER_IDLE` it closes.
  (`WebSocketClient$channelPoolHandler$1.java:106-126`).
  Default timeouts (`EmbeddedConnectionConfig.java:26`):
  `readTimeoutMillis = 10000`, `writeTimeoutMillis = 3000`,
  `connectTimeoutMillis = 2000`. So the client pings every ~3 s of
  outbound idleness and tears down after ~10 s of no inbound traffic.
- TV-side idle close: NOT-FOUND in client code; can only be inferred from
  the 10 s read-timeout on the client side.
- Pong handling: logged and ignored (`WebSocketClientHandler.java:134-136`).
- Close frame: client closes its channel on receipt
  (`WebSocketClientHandler.java:137-139`).

## Subscription protocol

### Register envelope — CONFIRMED (sent over HTTP, not WS)

`com/vizio/vnf/swagger/apis/V2SCPWebsocketApi.java:31-36`:

```
public final Request<ClientResponse> eventRegister(String AUTH, Body body) {
    String json = Serializer.getGson().toJson(body);
    return new Request<>(new RequestConfig(
        RequestMethod.PUT, "/event/register", null,
        MapsKt.mapOf(TuplesKt.to("AUTH", AUTH)), …),
        json, ...);
}
```

Body model (`com/vizio/vnf/swagger/models/Body.java:10-13`):

```
class Body {
    private final String REQUEST;     // "MODIFY"
    private final String VALUE;       // null in this call
    private final Long   HASHVAL;     // null in this call
}
```

Payload as built by `DeviceCommandBuilder.java:360`:

```
new DeviceCommand(V2SCPWebsocketApi.INSTANCE.eventRegister(
    authToken, new Body("MODIFY", null, null, 6, null)), retry);
```

Wire JSON: `{"REQUEST":"MODIFY"}` — and that is the **entire** body. There
is no URI / cname / topic on this request. It is a single global toggle
that says "this AUTH token wants events."

The HTTP agent that sends this is the *same* HTTP agent used for normal
REST commands — confirmed at
`WebsocketConnectionStrategy$establishWebsocketConnection$2$1$1.java:82-87`:

```
Agent agent = $httpAgent;
Command<ClientResponse> cmd = $registerCommand;
agent.send(cmd, …)              // <-- HTTP
```

Then, only after `registerResult.getSuccess()`, the WS is opened
(line 94-102 of same file):

```
if (registerResult.getSuccess()) {
    Agent.connect$default($websocketAgent, …)   // <-- WS upgrade
}
```

Timeout for the whole register-and-connect sequence:
`WebsocketConnectionStrategy.TIMEOUT_MILLIS_WS_REGISTER = 3000` (line 39).

### Unregister — UNCLEAR

Closing the WebSocket appears to be the de-facto unsubscribe — the strategy
just `Job.cancel`s the listener (`WebsocketConnectionStrategy.java:202`,
`DeviceWebsocketMonitor.java:188-194`). I find no `eventUnregister` /
`REQUEST: REMOVE` symbol anywhere in the decompile. The `Body` model does
support `REQUEST` as an arbitrary string, but the only call site uses
`"MODIFY"`.

### Wildcard / per-cname — CONFIRMED implicit-wildcard

There is **no per-cname subscribe**. After `MODIFY`, the TV pushes events
for whatever properties it considers observable. The Android client filters
client-side by URI (`processor.getUri()` — see "Subscribable cnames"
below). This means a Python client should *not* try to send "register
volume" / "register input" frames; just register once and demultiplex by
the inbound `URI` field.

## Event payload shape — CONFIRMED (envelope) / LIKELY (full body)

The frame is a `TextWebSocketFrame` whose body is JSON. The only field
the client deserializes generically is `URI`
(`com/vizio/connectivity/models/WebsocketMessageWrapper.java:14-15`):

```
@SerializedName("URI")
private final String uri;
```

Every processor then re-parses the same string. The processors' decoded
bodies (e.g. `WebsocketPowerModeProcessor`, `CurrentVolumeMessageProcessor`)
are unfortunately the unreadable "method dump skipped" jadx blocks, so the
exact JSON beyond `URI` cannot be cited verbatim. Based on the constant
strings the processors compare against (the `getUri()` values match REST
`URI` paths byte-for-byte) and the fact that the same `Serializer.getGson()`
is reused, the strong inference is:

```
{
  "URI":     "state/device/power_mode",
  "STATUS":  {"RESULT": "SUCCESS", "DETAIL": "..."},
  "ITEMS":   [ { "CNAME": "...", "VALUE": <new value>, "HASHVAL": 12345, ...} ],
  "HASHLIST":[…],
  "PARAMETERS":{"FLAT":"TRUE","HELPTEXT":"FALSE"}
}
```

i.e. the same envelope the REST API returns when you GET that URI — the
broadcast is functionally a "free" GET. This is consistent with the
strategy code that pipes WS messages through the same processors that
handle polled REST responses (`DeviceWebsocketMonitor.java:56`,
`WebsocketConnectionStrategy.java:198-255`). FLAGGED — verify on a real
device; the verbatim shape past `URI` is not directly visible in the
decompile.

`HASHVAL` is at minimum supported in the request `Body` model and is
referenced by every REST cache layer in the existing notes, so it is very
likely echoed back in the event payload.

## Subscribable cnames — CONFIRMED (subset)

The Android app explicitly de-multiplexes only these five URIs
(`DeviceWebsocketMonitor.java:56`):

| URI                                                | Source / processor |
|----------------------------------------------------|--------------------|
| `state/device/power_mode`                          | `WebsocketPowerModeProcessor.java:10` (`DeviceApiRoutes.GET_POWER_STATE`) |
| `app/current`                                      | `CurrentAppMessageProcessor.java:15` (`DeviceApiRoutes.GET_CURRENT_APP`) |
| `system/context_change`                            | `CurrentInputMessageProcessor.java:10` |
| `audio/volume/level`                               | `CurrentVolumeMessageProcessor.java:9` |
| `audio/volume/mute`                                | `CurrentMuteMessageProcessor.java:9` |

Notes:

- `system/context_change` is the input-change event — note the URI is
  *not* the same as REST `system/input/current_input`. CONFIRMED but
  surprising — flag for verification.
- The TV may emit other URIs; the Android app simply ignores them. There
  is no "list of subscribable cnames" advertisement anywhere in the code.
- `menu_native/dynamic/...` settings: NOT-FOUND. No processor watches the
  settings tree, so we don't know whether the TV broadcasts those.
- Soundbar/audio-only path: `DeviceWebsocketMonitor.startMonitor()` only
  runs the WS strategy when `deviceType == VIZIO_TV` and the device is
  not a Marvell TV (`DeviceWebsocketMonitor.java:70-74`). Audio devices
  use the WS for voice streaming, not events.

## Capability advertisement — NOT-FOUND

There is no `SCPL_CAPABILITIES.websocket` flag. The Android app gates the
WS pipeline on three runtime checks instead:

1. mDNS TXT record contains `wsPort` or `wssPort`
   (`DnsVizioTxtMessageParser.java:178,313`).
2. `internalDevice.getAuthToken() != null` and HTTP agent + WS agent both
   built (`WebsocketConnectionStrategy.java:179-182`):

   ```
   return authToken != null && authToken.length() > 0
          && agents.getHttp() != null && agents.getWebSocket() != null;
   ```

3. Device is a TV and is not a Marvell-SoC TV
   (`DeviceWebsocketMonitor.java:70-74`).
4. The `PUT /event/register` HTTP call returns success
   (`WebsocketConnectionStrategy$establishWebsocketConnection$2$1$1.java:90-93`).

Implication for `pyvizio`: detect support by *probing*
`PUT /event/register {"REQUEST":"MODIFY"}` and falling back to polling if
that fails or the WS upgrade is rejected. The mDNS TXT-record key
(`wsPort`/`wssPort`) is the cheapest pre-check.

Minimum firmware version: NOT-FOUND.

## Reconnect — CONFIRMED

- `DeviceCommandBuilder.eventRegister` defaults to a `Retry(1, 0L, 0.0d, 0L, 0L)`
  — i.e. one attempt, no backoff (`DeviceCommandBuilder.java:352`).
- The outer monitor loop reconnects with a flat 15 s delay, no
  exponential backoff: `DeviceWebsocketMonitor.DELAY_SETUP_RETRY_MS = 15000`
  (`DeviceWebsocketMonitor.java:40`), looped while
  `CoroutineScopeKt.isActive(scope)` (lines 152-171). On every cycle the
  full strategy reruns, which means **the client re-sends
  `PUT /event/register` and re-opens the WS** every time. The TV does *not*
  remember the registration across reconnects; assume the same.
- Events that fire while disconnected: dropped. There is no replay-on-reconnect
  in the Android code; the strategy just resumes listening.
- Channel-level error → cancel the listener job, propagate to the
  pipeline scope, the 15 s loop kicks in
  (`WebSocketClient.java:196-203`, `WebSocketClient$errorListener$1$1`).

## Cross-references with REST

- The `Body` envelope (`REQUEST`/`VALUE`/`HASHVAL`) is shared with REST;
  see `Body.java`. REST uses `MODIFY` for writes too.
- The `AUTH` header is reused identically.
- The WS upgrade adds `Authorization` (capital-A, full word) *in
  addition to* the `?TOKEN=` query param; this is a **new** auth surface
  not present on REST. Possible v3 firmware behavior. **FLAGGED** for
  verification.
- TLS posture matches REST: self-signed accepted via
  `InsecureTrustManagerFactory`.

## Recommendations for vizio-smartcast

Concrete API shape (proposed):

```python
async with vizio.subscribe_events() as events:   # AsyncContextManager
    async for event in events:                   # AsyncIterator[VizioEvent]
        # event.uri           : str   (e.g. "audio/volume/level")
        # event.value         : Any   (parsed from ITEMS[0].VALUE)
        # event.hashval       : int | None
        # event.raw           : dict  (full envelope)
        ...
```

Implementation notes:

1. **Bootstrap**: derive `ws_port` / `wss_port` from mDNS TXT during
   discovery, or expose as constructor kwargs (matching the existing
   `port` arg). Default to the REST port + scheme switch is *not* safe —
   the WS port is genuinely separate (e.g. 7345 for audio, 9000-range
   for TVs). If unknown, probe `wss://<host>:7345/` then `:9000/`.
2. **Register, then connect**: issue `PUT /event/register` with
   `{"REQUEST":"MODIFY"}` and the existing `AUTH` header through the
   normal REST stack. If 200, open the WS; otherwise raise
   `VizioWebSocketUnsupported` (new exception).
3. **Single connection, multiplex client-side**: do not send any frame
   on the socket after the upgrade — just read text frames, JSON-parse,
   dispatch by `URI`. Mirror the five known URIs but do not filter
   unknown URIs out — surface them so callers can opt in to others.
4. **Heartbeat**: rely on `aiohttp.WSMsgType.PING/PONG` autohandling, but
   set `heartbeat=3.0` and `receive_timeout=10.0` to match the Android
   client's `IdleStateHandler` numbers.
5. **Reconnect**: simple loop with the same 15 s delay; on each
   reconnect re-issue `PUT /event/register`. Document that events during
   the gap are lost; suggest callers do an initial state read on
   (re)connect to reconcile.
6. **TLS**: reuse the existing `ssl=False` / `verify=False` mechanism.
7. **Capability check**: there is no flag to test. Either expose
   `subscribe_events()` unconditionally and let it raise, or do a one-shot
   probe (`PUT /event/register`) and cache the result on the `Vizio` object.
8. **Voice / audio-stream WS**: the `BinaryWebSocketFrame` path
   (`WebsocketAudioStreamMessage`) is a *different* feature on the *same*
   port. Confirm we are not conflating: the SCPL event channel is text
   only; binary frames belong to voice and should be ignored (or rejected
   loudly) by the events client. CONFIRMED separate by
   `WebSocketClient.java:153-166`.

## Code excerpts (for the record)

`com/vizio/vnf/swagger/apis/V2SCPWebsocketApi.java:31-36`

```java
public final Request<ClientResponse> eventRegister(String AUTH, Body body) {
    String json = Serializer.getGson().toJson(body);
    return new Request<>(new RequestConfig(RequestMethod.PUT,
        "/event/register", null,
        MapsKt.mapOf(TuplesKt.to("AUTH", AUTH)), MapsKt.emptyMap(),
        4, null), json, ...);
}
```

`com/vizio/vnf/network/agent/DeviceCommandBuilder.java:357-361`

```java
public final Command<ClientResponse> eventRegister(String authToken, Retry retry) {
    return new DeviceCommand(
        V2SCPWebsocketApi.INSTANCE.eventRegister(
            authToken, new Body("MODIFY", null, null, 6, null)),
        retry);
}
```

`com/vizio/vnf/network/agent/websocket/WebSocketClient.java:92-100`

```java
String scheme = embeddedConnectionConfig.getRouteConfig().getScheme();
InetAddress address = embeddedConnectionConfig.getRouteConfig().getAddress();
uri = new URI(scheme + "://" + address.getHostName() + ":" +
    embeddedConnectionConfig.getRouteConfig().getPort() +
    embeddedConnectionConfig.getRouteConfig().getPath() +
    (embeddedConnectionConfig.getAuthToken() != null
       ? "?TOKEN=" + embeddedConnectionConfig.getAuthToken() : ""));
```

`com/vizio/vnf/network/agent/websocket/WebSocketClient$channelPoolHandler$1.java:80-85`

```java
DefaultHttpHeaders headers = new DefaultHttpHeaders();
headers.add(SmartCastHeaders.HEADER_KEY_SMARTCAST_SOURCE,
            SmartCastHeaders.SMARTCAST_SOURCE_ANDROID);
for (Map.Entry<String,String> e :
        connectionConfig.getHeaders().entrySet())
    headers.add(e.getKey(), e.getValue());
WebSocketClientHandshaker shaker =
    WebSocketClientHandshakerFactory.newHandshaker(
        uri, WebSocketVersion.V13, null, true, headers, 1048576);
```

`com/vizio/vnf/network/agent/websocket/WebSocketClient$channelPoolHandler$1.java:106-126`

```java
pipeline.addLast("idleStateHandler", new IdleStateHandler(
    readTimeoutMillis, writeTimeoutMillis, 0L, MILLISECONDS));
pipeline.addLast("readAndWriteIdleHandler", new ChannelDuplexHandler() {
    public void userEventTriggered(ctx, evt) {
        if (evt instanceof IdleStateEvent ev) {
            if (ev.state() == READER_IDLE) ctx.close();
            else if (ev.state() == WRITER_IDLE)
                ch.writeAndFlush(new PingWebSocketFrame(Unpooled.buffer(0)));
        }
    }
});
```

`com/vizio/vdf/services/manager/strategies/WebsocketConnectionStrategy$establishWebsocketConnection$2$1$1.java:82-102`

```java
// 1. PUT /event/register over HTTP
agent.send(registerCommand, …)        // $httpAgent.send(...)
// 2. only on success, upgrade WS
if (registerResult.getSuccess()) {
    Agent.connect$default($websocketAgent, …)
}
```

`com/vizio/vdf/services/manager/polling/DeviceWebsocketMonitor.java:40,56,67-74`

```java
private static final long DELAY_SETUP_RETRY_MS = 15000;
defaultProcessors = listOf(
    new WebsocketPowerModeProcessor(),
    new CurrentAppMessageProcessor(),
    new CurrentInputMessageProcessor(),
    new CurrentVolumeMessageProcessor(),
    new CurrentMuteMessageProcessor());
…
if (deviceType == VIZIO_TV
        && !new DeviceInfoAnalyzer(…).isMarvellTv()) {
    activeUpdaterJob = launch { strategy.runStrategy() }
}
```

`com/vizio/connectivity/models/WebsocketMessageWrapper.java:14-15`

```java
@SerializedName("URI")
private final String uri;
```
