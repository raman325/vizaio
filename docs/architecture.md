# Architecture walkthrough

A guided tour of `vizaio` for someone reading the codebase for
the first time. If you're trying to follow a request from "user calls a
method" to "bytes on the wire" — this is the order to read in.

The design is deliberately layered. Each module has one responsibility,
modules below have no idea modules above exist.

```
┌──────────────────────────────────────────────────────────────────┐
│  cli/  (TODO)                       — typer entry points         │
├──────────────────────────────────────────────────────────────────┤
│  Vizio class           (_device.py) — public API; capability     │
│                                       gating; hashval recovery;  │
│                                       pair_session context mgr   │
├──────────────────────────────────────────────────────────────────┤
│  apps.py                            — bundled+remote app catalog │
│  discovery.py                       — zeroconf + SSDP            │
├──────────────────────────────────────────────────────────────────┤
│  SmartCastClient       (_client.py) — HTTP transport             │
│                                       (auth, SSL, fallback paths)│
├──────────────────────────────────────────────────────────────────┤
│  resolve()           (_endpoints.py) — Endpoint enum + per-      │
│                                       profile EndpointSpec       │
│  _payloads.py                       — PUT body builders          │
├──────────────────────────────────────────────────────────────────┤
│  Response  (_wire.py) — case-normalized envelope; Item dataclass │
│  _parse.py            — high-level Response → typed-result helpers│
├──────────────────────────────────────────────────────────────────┤
│  DeviceProfile / DeviceType  (_types.py, _profiles.py, _keys.py) │
│  VizioError + subclasses                          (_errors.py)   │
└──────────────────────────────────────────────────────────────────┘
```

## A request from top to bottom

Take a single call: `await vizio.power_on()` on a TV.

1. **`Vizio.power_on()`** delegates to `Vizio.send_key("POW_ON")` —
   `_device.py`, which is the only "smart" layer in the library.
2. **`Vizio.send_key`** looks up `("POW_ON" → (codeset, code))` in the
   keymap on the device's `DeviceProfile`. If the key isn't in the
   keymap (e.g., `CH_UP` on a soundbar), it raises `VizioUnsupportedError`
   immediately, no HTTP.
3. It then calls `_payloads.key_press([(codeset, code)])` to build
   `{"KEYLIST": [{"CODESET": ..., "CODE": ..., "ACTION": "KEYPRESS"}]}`.
   Payload builders are pure data — no I/O, no auth, no validation.
4. **`Vizio._request(Endpoint.KEY_PRESS)`** calls `resolve(KEY_PRESS, profile)`
   from `_endpoints.py` to get an `EndpointSpec`:
   `{paths: ("/key_command/",), method: "PUT", auth: REQUIRED, item_cname: None}`.
   The resolver is a registry of `Endpoint → fn(profile) → EndpointSpec`,
   so the same enum produces different specs for different device types.
5. `_check_auth(spec.auth)` runs at the Vizio level — if the endpoint
   needs auth and we don't have a token, raise `VizioAuthError` here
   before any HTTP work. (Defense in depth: `SmartCastClient` checks
   again, but mocking-friendly tests need the upper-layer check too.)
6. **`SmartCastClient.request_spec(spec, body=...)`** is the HTTP layer
   in `_client.py`. It iterates `spec.paths` (only one for `KEY_PRESS`,
   but multiple for ESN/serial/version per APK quirk #3). For each path:
   constructs `https://{host}{path}`, adds `AUTH` header (per
   `spec.auth` and configured token), adds `VIZIO-SmartCast-Source`
   header, sends the request with `ssl=False` (devices use self-signed
   certs).
7. The response goes through **`Response.from_json(data)`** in `_wire.py`
   — the boundary that lowercases all keys (per APK quirk #1) and
   builds `Item` dataclasses. Status mapping turns `STATUS.RESULT` into
   the `ResponseStatus` enum and translates non-success results into
   typed exceptions.
8. Vizio.power_on returns `None`. Caller proceeds.

A getter (e.g., `await vizio.get_power_state()`) is the same shape but
the response gets passed to `Response.require_item("power_mode")` which
returns a typed `Item`, and the caller pulls `item.value`.

## Key design choices and why

### Layered boundaries

- **`Response.from_json` is the wire boundary.** Lowercasing happens
  exactly once. Everything above it sees clean lowercase keys; nothing
  above it has to defend against mixed casing. (The `_ci_get` helper
  pyvizio uses everywhere — gone.)
- **`EndpointSpec` is the dispatch unit.** No call site says "GET this
  URL"; they say `resolve(Endpoint.X, profile)`. This is what makes
  capability gating cheap (resolver raises `VizioUnsupportedError`
  before any network call) and what makes firmware path fallback live
  in the right place.

### Capability profiles instead of a device-type enum tree

- **`DeviceProfile`** carries `settings_root`, `max_volume`,
  `requires_auth`, `has_battery`, `has_inputs`, `has_apps`, `keymap`.
- **`DeviceType`** is a convenience selector that resolves to a built-in
  profile preset. Users can construct custom profiles for unusual or
  future devices (e.g., when Vizio ships a new soundbar variant) without
  waiting for a library update.

### Pairing as a context manager

- pyvizio's three-method (`start_pair`, `pair`, `stop_pair`) flow
  required callers to remember `cancel_pair` on every error path. Many
  forgot, leaving devices stuck.
- `vizio.pair_session(...)` is an `async with` that auto-cancels on any
  exit path *except* a successful `complete()`.

### Hashval race recovery

- pyvizio's `set_setting` does GET-then-PUT for the hashval. If the
  hashval changes between the two requests, the PUT fails — that's
  open issues #135 and #140.
- We detect the failure (`VizioInvalidParameterError` from a setting
  PUT we issued without a caller-supplied hashval), refetch, and retry
  *once*. Caller sees no exception unless the second attempt also fails.
- Caller-supplied hashvals don't auto-retry — caller has full control.

## Working examples

### 1. Connect, get state, power off

```python
import asyncio
from vizaio import DeviceType, Vizio, VizioError

async def main():
    async with Vizio(
        host="192.168.1.50",
        device_type=DeviceType.TV,
        auth_token="Z3pndnpncGV2",
    ) as vizio:
        # Quick connectivity check.
        await vizio.ping_auth()           # raises VizioAuthError if token rejected

        # Read state.
        on = await vizio.get_power_state()
        volume = await vizio.get_volume()
        muted = await vizio.is_muted()
        current_input = await vizio.get_current_input()
        print(f"on={on} vol={volume} muted={muted} input={current_input}")

        # Issue a command (returns None on success, raises on failure).
        if on:
            await vizio.power_off()

asyncio.run(main())
```

Things to notice:

- The `async with` block is the lifecycle. `Vizio` owns its aiohttp
  session unless you pass one in.
- Getters return typed values (`int`, `bool`, `str`). They raise
  `VizioError` on failure — never `None`.
- Actions return `None` on success. Same exception story.

### 2. Discovery → pair → use

```python
import asyncio
from vizaio import DeviceType, Vizio
from vizaio.discovery import discover

async def setup():
    devices = await discover(timeout=5.0)
    if not devices:
        raise RuntimeError("no Vizio devices found on the LAN")

    target = devices[0]
    print(f"Found: {target.name} at {target.host} ({target.model})")

    # Pair — async context manager guarantees cleanup if anything goes wrong.
    async with Vizio(host=target.host, device_type=DeviceType.TV) as v, \
               v.pair_session(
                   device_id="my-script",
                   device_name="My Setup Script",
               ) as session:
        print("PIN should now be visible on the TV screen.")
        pin = input("Enter PIN: ")
        auth_token = await session.complete(pin=pin)
        print(f"Auth token: {auth_token!r}")

    # Reuse the token to connect for real.
    async with Vizio(
        host=target.host,
        device_type=DeviceType.TV,
        auth_token=auth_token,
    ) as v:
        await v.power_on()

asyncio.run(setup())
```

Things to notice:

- `pair_session` is the *only* way to pair. There's no exposed
  `start_pair` / `cancel_pair` — the context manager handles both ends.
- If the user Ctrl-C's at the PIN prompt, or if `complete()` raises
  (wrong PIN), the context manager calls `cancel_pair` on its way out.
  The TV doesn't get stuck in pairing mode.
- Discovery returns plain `DiscoveredDevice` records — no library state,
  free to build a UI from.

### 3. Send key presses

```python
from vizaio import RemoteKey

async def navigate(vizio):
    # Single press by enum (preferred — type-checked).
    await vizio.send_key(RemoteKey.MENU)
    await vizio.send_key(RemoteKey.DOWN)
    await vizio.send_key(RemoteKey.OK)

    # Or by string (works too; raises VizioUnsupportedError for invalid keys).
    await vizio.send_key("INFO")

    # Hold-equivalent: the device's volume is hardware-rate-limited, so
    # sending VOL_UP three times via volume_up(steps=3) is one PUT with
    # a 3-element KEYLIST. The library chunks at 50 if you ask for more.
    await vizio.volume_up(steps=10)

    # Convenience methods for the most common keys.
    await vizio.power_on()
    await vizio.mute()
    await vizio.next_input()
```

Things to notice:

- `vizio.available_keys` is a `frozenset[str]` on the device profile.
  You can ask the library what keys it knows about for *this* device:

  ```python
  if "CH_UP" in vizio.available_keys:
      await vizio.send_key("CH_UP")
  ```

- Soundbars/Crave devices don't have channel keys, so `send_key("CH_UP")`
  raises on those — *before* the HTTP call goes out.

### 4. Settings — read all, write one efficiently

```python
async def adjust(vizio):
    # Read the whole audio category — values + options merged into
    # SettingInfo dataclasses.
    audio = await vizio.get_settings("audio")
    print(audio["volume"].value)        # 25 (int)
    print(audio["volume"].min)          # 0
    print(audio["volume"].max)          # 100
    print(audio["volume"].hashval)      # opaque server token

    print(audio["eq"].options)          # ("Normal", "Music", "Movie", "Game")

    # Write efficiently: pass the cached hashval to skip the per-write GET.
    # If the hashval is stale (race), the library does NOT auto-retry —
    # caller has full control.
    await vizio.set_setting(
        "audio", "volume", 30,
        hashval=audio["volume"].hashval,
    )

    # Or write naively: one GET, one PUT. On stale-hashval failure we
    # retry the GET+PUT once before raising.
    await vizio.set_setting("audio", "eq", "Music")
```

Things to notice:

- The HA pattern: a coordinator polls `get_settings("audio")` once per
  scan interval, caches the dict, then dispatches volume / mute / EQ
  changes against the cached hashvals — one round trip per write
  instead of two.
- Setting writes that fail with `invalid_parameter` get one auto-retry
  to handle the hashval race documented in protocol-notes #13.

### 5. Apps

```python
async def show_app(vizio):
    name = await vizio.get_current_app()
    if name is None:
        print("No app running (TV is on an HDMI input or off)")
    else:
        print(f"Currently watching: {name}")

    # Launch by name. Looks up against the bundled+remote-fetched catalog.
    await vizio.launch_app("Netflix")
```

### 6. Catching failures

```python
from vizaio import (
    VizioAuthError,
    VizioBusyError,
    VizioConnectionError,
    VizioError,
    VizioInvalidInputError,
    VizioInvalidParameterError,
    VizioUnsupportedError,
)

async def safe(vizio):
    try:
        await vizio.set_input("HDMI-99")
    except VizioInvalidInputError as e:
        # Includes the valid input names in the error message.
        print(f"Bad input choice: {e}")
    except VizioInvalidParameterError as e:
        # Device rejected the value (e.g., setting volume to 9999).
        print(f"Bad value: {e}")
    except VizioConnectionError as e:
        # Network unreachable, timeout, TLS failure.
        print(f"Cannot reach device: {e}")
    except VizioAuthError as e:
        # Token missing, invalid, or rejected.
        print(f"Auth problem: {e}")
    except VizioUnsupportedError as e:
        # Tried something the device doesn't support
        # (e.g., set_input on a soundbar).
        print(f"Not supported on this device: {e}")
    except VizioError as e:
        # Catch-all parent — any other VizioError subclass.
        print(f"Generic device problem: {e}")
```

Inheritance graph:

```
VizioError
├── VizioConnectionError
├── VizioAuthError
├── VizioResponseError
├── VizioInvalidParameterError
│   └── VizioInvalidInputError    ← raised by set_input("nonexistent")
├── VizioNotFoundError
├── VizioBusyError                ← maps from STATUS.RESULT == BLOCKED
└── VizioUnsupportedError         ← raised by capability gating BEFORE HTTP
```

`VizioInvalidInputError` is a subclass of `VizioInvalidParameterError`
on purpose — code that catches "device said no" can stay broad, code
that wants to recover specifically from "wrong input name" can narrow.

## WebSocket SCPL — `subscribe_events()` (v0.2-alpha)

The library also speaks the device's WebSocket SCPL protocol for
push-based state updates. This sits alongside the REST surface — same
auth token, same host, but a different port (advertised via mDNS as
`wsPort`/`wssPort`, fallback 7345).

### Why it exists

Polling for power/volume/mute/input/app state via REST has two costs:

- Latency — physical-remote presses don't reflect in HA until the next
  scan interval (~10s).
- Device load — each scan cycle is a burst of REST round trips. APK
  research suggests this saturation pattern explains the "TV stops
  responding" issue (#175 in pyvizio).

A long-lived WebSocket eliminates both. The TV pushes a frame whenever
power, volume, mute, current input, or current app changes; HA reacts
in ~50ms with one connection.

### How the protocol works

**Surprise discovery:** `eventRegister` is **not a WebSocket frame**.
The official Android app sends `PUT /event/register` over the regular
REST agent first, then opens the WebSocket. There is no per-cname
subscription on the wire — just a single global "send me events"
toggle.

```
Client                                          Device
  |  PUT /event/register {"REQUEST": "MODIFY"}   |
  |  AUTH: <token>                               |
  |--------------------------------------------->|
  |                                  200 SUCCESS |
  |<---------------------------------------------|
  |                                              |
  |  WS upgrade GET /?TOKEN=<token>              |
  |  Authorization: <token>                      |
  |  VIZIO-SmartCast-Source: vizaio     |
  |--------------------------------------------->|
  |                          101 Switching ...   |
  |<---------------------------------------------|
  |                                              |
  |          (TV pushes JSON text frames)        |
  |<---------------------------------------------|
  |       {"URI": "audio/volume/level", ...}     |
  |       {"URI": "state/device/power_mode",...} |
  |  PING                                        |    every 3s of idle
  |--------------------------------------------->|
  |                                         PONG |
  |<---------------------------------------------|
  |                                              |
  |        ... (10s read-idle → close)           |
```

**Auth quirk:** REST uses an `AUTH` header. WS uses `Authorization`
(capital-A) PLUS the auth token as a `?TOKEN=` query param on the
upgrade URL. We send both, matching the Android app exactly.

**Reconnect:** On disconnect, the strategy is identical — send
`PUT /event/register` again, open a new WS. The TV doesn't remember
subscriptions across drops. Default backoff is a flat 15s loop (no
exponential). Events that fire while disconnected are dropped (no
replay).

### Subscribable URIs

The Android app demultiplexes exactly 5 URIs (`KNOWN_URIS` in
`_websocket.py`):

```python
"state/device/power_mode"         # POW_ON / POW_OFF
"app/current"                     # SmartCast app launches
"system/context_change"           # input changes (NOT system/input/current_input)
"audio/volume/level"              # volume setting
"audio/volume/mute"               # mute toggle
```

The TV may emit other URIs; the official app silently ignores them.
We surface them all — `StateEvent.uri` is just whatever the device
sent. Filter on the caller side.

### Public API

```python
from vizaio import Vizio, DeviceType

async with Vizio(host=..., device_type=DeviceType.TV, auth_token=...) as v:
    async with v.subscribe_events() as events:
        async for event in events:
            print(f"{event.uri} -> {event.value} (cname={event.cname})")
```

`StateEvent` carries:

- `uri` — the path the device emitted (raw string)
- `value` — best-effort typed value (int, str, dict, or None if we
  couldn't determine one)
- `cname` — first item's cname, if the inferred shape matched
- `hashval` — first item's hashval, when present (lets HA cache for
  efficient writes)
- `raw` — the lowercased original envelope (escape hatch)

Options:

- `auto_reconnect=True` (default) — keep yielding across disconnects
- `auto_reconnect=False` — iterator ends on first close
- `reconnect_delay=15.0` (default) — flat seconds between attempts
- `ws_port=N` — override the WS port (rarely needed)

### What's verified vs. inferred

This is the part to read carefully if you're building on the WS API.

**Verified from the APK source (high confidence):**

- URL scheme, port discovery, path (`/`)
- Auth surface (`?TOKEN=` + `Authorization` header)
- Heartbeat: ping every 3s write-idle, close after 10s read-idle
- `PUT /event/register` with `{"REQUEST": "MODIFY"}` body
- Reconnect cycle (re-register every time)
- The 5 demultiplexed URIs
- Voice streaming uses the same WS port with binary frames (we ignore
  binary frames — this library doesn't do voice)

**Inferred from context, NOT directly visible in bytecode:**

- The exact event-payload JSON shape past the `URI` field. The
  per-processor JSON deserializers were "method dump skipped" blocks
  in jadx output. The strong inference is the same `STATUS`/`ITEMS`
  envelope as REST, since the same `Serializer.getGson()` is reused
  and the processors that handle WS frames are the same ones that
  handle REST responses. **HW-VERIFY:** if the real device emits a
  different shape, we adjust `_parse_event_frame` and re-test.

**Unknown until hardware:**

- Whether settings tree (`menu_native/dynamic/...`) emits events at
  all
- TV-only vs. soundbar/Crave behavior (the Android app gates this on
  `device_type == TV`; we don't enforce — we probe and fall back to
  polling on register failure)
- Whether the TV has any rate limit on event throughput
- Behavior under simultaneous WS + REST load (e.g., does a setting
  PUT trigger an immediate event?)

The test suite (`tests/test_websocket.py`, 27 tests) covers the
verified parts plus the inferred-shape assumption with synthetic
frames mirroring our best guess. Look for `# HW-VERIFY` markers when
revisiting after hardware testing.

### Module structure

```
_websocket.py
├── EVENT_REGISTER_PATH / EVENT_REGISTER_BODY  (constants)
├── KNOWN_URIS                                 (the 5 documented URIs)
├── SubscribeOptions                           (frozen knobs dataclass)
├── EventStream                                (async ctx mgr + iterator)
│   ├── __aenter__ / __aexit__
│   ├── __aiter__  → _iterate()
│   ├── _connect()                              (register → ws_connect)
│   ├── _register_for_events()                  (REST PUT)
│   ├── _consume_frames()                       (one event per text frame)
│   ├── _build_ws_url() / _build_ws_headers()
│   └── aclose()
└── _parse_event_frame()                       (JSON-in / StateEvent-out)
```

The `Vizio.subscribe_events()` method is a thin factory — all the
logic lives in `EventStream`. This keeps `_device.py` clean and makes
the WS path independently testable.

### Falling back when WS isn't available

The library doesn't auto-detect WS support — it probes by trying. If
`PUT /event/register` returns an error, the connect attempt raises
`VizioConnectionError`. Callers wanting a graceful fallback to polling
should catch:

```python
async def watch(vizio):
    try:
        async with vizio.subscribe_events() as events:
            async for event in events:
                ...
    except VizioConnectionError:
        # WS unavailable — fall back to polling
        while True:
            state = await vizio.get_power_state()
            ...
            await asyncio.sleep(10)
```

A future v0.3 may add `Vizio.has_websocket_support()` after we have
hardware data on which firmware revisions support it reliably.

---

## Reading order for the codebase

If you want to read your way through the source from the bottom up:

1. **`_errors.py`** — small, sets the exception vocabulary.
2. **`_types.py`** — `DeviceType`, `DeviceProfile`, `InputInfo`,
   `SettingInfo`, `AppConfig`. Pure data.
3. **`_keys.py`** — remote key catalogs per device family.
4. **`_profiles.py`** — three (now five with Crave variants) preset
   `DeviceProfile` instances.
5. **`_wire.py`** — `Item` and `Response`. The wire boundary.
6. **`_payloads.py`** — PUT body builders. Pure data, mirror of
   `_wire.py`.
7. **`_endpoints.py`** — `Endpoint` enum, `EndpointSpec` dataclass,
   path builders, and the `resolve(endpoint, profile)` registry. The
   most important file in the codebase for understanding the protocol.
8. **`_parse.py`** — higher-level helpers built on `Response`. Inputs
   filtering, settings merging, app config extraction.
9. **`_client.py`** — `SmartCastClient`. The HTTP layer. Owns the
   aiohttp session, applies auth, walks `EndpointSpec.paths` for
   firmware-fallback.
10. **`apps.py`** — bundled JSON + remote refresh + `find_app_name`.
11. **`discovery.py`** — zeroconf + SSDP.
12. **`_websocket.py`** — `EventStream` + `_parse_event_frame`. The
    push-side counterpart to `_client.py`. Independent of `_device.py`
    until you reach `Vizio.subscribe_events()`.
13. **`_device.py`** — `Vizio` class, the public API. This is the only
    file in the package that touches all the others.

## Reading order for the test suite

The tests are organized to support that same bottom-up reading. Each
test file pins down the contract for one module:

- `tests/_fixtures.py` — JSON-shape factories (no production code
  dependency)
- `test_errors.py` — exception hierarchy invariants
- `test_payloads.py` — PUT body shapes
- `test_wire.py` — `Response.from_json` boundary
- `test_endpoints.py` — path builders + resolver
- `test_profiles.py` — capability profiles
- `test_apps.py` — app catalog + matching
- `test_discovery.py` — zeroconf + SSDP
- `test_device.py` — the `Vizio` class methods, with `SmartCastClient`
  mocked
- `test_websocket.py` — `EventStream` + `_parse_event_frame`, with
  aiohttp's `ws_connect` mocked

If a test fails after a refactor, the location pins down what layer
the bug is in.

## Where to start when adding a feature

| Adding... | Edit |
|-----------|------|
| A new remote key | `_keys.py` (the per-device keymap) |
| A new endpoint | `_endpoints.py` (add `Endpoint` member + `_resolve_*` fn + register in `_RESOLVERS`) |
| A new device family | `_profiles.py` (add a `DeviceProfile` preset) + `_keys.py` (its keymap) |
| A new wire-format quirk | `_wire.py` (extend `Response.from_json` or `_item_from_dict`) |
| A new public method | `_device.py` (the only place public API lives) |
| A new exception subclass | `_errors.py` |
| A new fixture for tests | `tests/_fixtures.py` |
| A new event field discovered on real hardware | `_types.py` (extend `StateEvent`) + `_websocket.py::_extract_item` |

## Summary

The library is bottom-heavy on purpose: most logic lives in pure-data
modules (`_types`, `_payloads`, `_wire`, `_endpoints`) that have no
dependencies on each other and can be unit-tested without HTTP. The
parts that *do* talk to the network (`_client`, `discovery`, `apps`)
live in tight, single-purpose modules. Only `_device.py` knows about
"a Vizio" as a coherent thing.

If you remember one thing: **`EndpointSpec` is the protocol contract**.
Reading the resolver in `_endpoints.py` tells you everything the
library can ask the device for.
