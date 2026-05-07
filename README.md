# vizaio

Modern async Python client and CLI for Vizio SmartCast devices — TVs, soundbars,
and Crave portable speakers. Successor to
[`pyvizio`](https://github.com/raman325/pyvizio); pyvizio is frozen and all new
work happens here.

- `vizaio` CLI with named device aliases, a default device, and TTY-aware output
  (rich tables on a terminal, TSV when piped, `--format json/plain` on demand).
- Pairing via subcommand group — `vizaio pair begin` + `pair complete`
  for scripting, `pair interactive` for human use — with auto-cancel if the PIN
  entry fails.
- Hashval-aware setting writes that survive the
  [pyvizio #135 / #140](https://github.com/raman325/pyvizio/issues/135) race.
- WebSocket event subscription (push-based state updates on supported TVs).
- Async-only Python library underneath, type-checked under `mypy --strict`,
  Python 3.12+.

---

## Contents

- [Install](#install)
- [Quickstart](#quickstart)
- [Pairing](#pairing)
- [CLI reference](#cli-reference)
- [Discovery](#discovery)
- [Device-type quirks](#device-type-quirks)
- [Library API](#library-api)
- [Exception hierarchy](#exception-hierarchy)
- [License](#license)

---

## Install

Requires **Python 3.12 or newer**.

```bash
# core (REST + WebSocket events + CLI)
uv add vizaio
# or: pip install vizaio

# core + zeroconf network discovery (optional)
uv add 'vizaio[discovery]'
# or: pip install 'vizaio[discovery]'
```

The `[discovery]` extra pulls in
[`zeroconf`](https://pypi.org/project/zeroconf/). Without the extra,
`vizaio discover` falls back to SSDP only (no extra dependency required, since
SSDP uses just the standard library plus the already-required `aiohttp`).

To install from source (e.g. for development):

```bash
git clone https://github.com/raman325/vizaio.git
cd vizaio
uv sync --all-extras
```

The `vizaio` console script is installed as a project entry point in either
case.

---

## Quickstart

First-time setup is three commands: discover, pair, and start using the
device.

```bash
# 1. Find devices on your network
vizaio discover

# 2. Pair (interactive — TV displays a PIN, you type it back)
#    --save-as stores the resulting auth token under an alias
vizaio pair interactive 192.168.1.50 --save-as livingroom

# 3. Make this the default so you can drop --device on every call
vizaio device set-default livingroom
```

After that, control the device:

```bash
vizaio power on
vizaio volume level
vizaio volume up --steps 5
vizaio input list
vizaio input set HDMI-2
vizaio app launch Netflix
```

Use the alias explicitly when you have more than one device:

```bash
vizaio --device kitchen-bar volume mute
```

---

## Pairing

Pairing is the same HTTP handshake on every device family — TVs, soundbars,
and Crave speakers all use `PUT /pairing/start`, `/pairing/pair`,
`/pairing/cancel`. There is no separate "V3 / WebSocket" pairing flow; the
WebSocket in this library is for event subscription **after** you already have
a token.

The `pair complete` and `pair interactive` subcommands return an
**auth token** — an opaque string sent on every authenticated REST call as a
literal `AUTH:` header (not `Authorization`, not `Bearer`). Tokens are:

- **Durable.** No documented TTL or refresh. Save the token; reuse it
  forever. Re-pair only if you reset the TV or change the `device_id` you
  paired with.
- **Bound to the `device_id` you supply at pairing time.** The CLI uses
  `vizio-cli` for that field. Re-pairing with the same `device_id`
  **invalidates the previous token immediately** (verified live:
  subsequent calls with the old token return raw HTTP 403, which the
  library surfaces as `VizioAuthError`). Use a stable, unique
  `device_id` per integration — don't pair twice expecting two
  parallel tokens.
- **Bearer credentials.** Treat them like passwords: never commit them, store
  with restrictive file permissions, scrub from logs. The CLI's config file
  is created with mode `0600` on Unix-like systems.

### CLI

```bash
# Interactive — prompts for PIN on stderr
vizaio pair interactive 192.168.1.50
vizaio pair interactive 192.168.1.50 --save-as livingroom

# Two-step (scriptable) — begin outputs challenge data + a ready-to-run
# "pair complete" command with all flags filled in except --pin
vizaio pair begin 192.168.1.50
# → challenge_type  pairing_token
# → 1               54321
# → Next step: vizaio pair complete 192.168.1.50 \
#     --challenge-type 1 --pairing-token 54321 --pin <PIN>

vizaio pair complete 192.168.1.50 \
    --challenge-type 1 --pairing-token 54321 --pin 1234 \
    --save-as livingroom

# Cancel a pairing session early (best-effort)
vizaio pair cancel 192.168.1.50

# Soundbars and Crave speakers don't require auth — skip pairing entirely
vizaio device add kitchen-bar --host 192.168.1.51 --device-type soundbar
```

`--device-type` defaults to `tv`. The interactive subcommand prompts for the
PIN on stderr; if the PIN is wrong, it cancels the pairing on the way out so
the TV doesn't get stuck in pairing mode. The `begin`/`complete` subcommands
are stateless — they make a single HTTP call each, making them safe for
scripts, CI, and non-interactive environments.

### Finding the device IP

```bash
vizaio discover                          # zeroconf + SSDP, 5s timeout
vizaio discover --timeout 10 --no-ssdp   # zeroconf only, 10s
```

Or programmatically — see [Discovery](#discovery).

### Common failure modes

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `vizaio: ... pairing` after entering PIN | Wrong PIN, or PIN entered after the device's pairing window expired | Re-run `vizaio pair interactive`; the previous attempt was already canceled |
| `VizioAuthError` on first call after pairing | Token saved correctly but `device_type` was wrong on the next call (e.g., paired a TV but the alias says `soundbar`) | Run `vizaio device show <alias>` and re-pair with the right `--device-type` |
| `vizaio: failed to reach https://...` during `pair begin` | Wrong port. TVs typically listen on **7345** (mDNS-advertised), occasionally **9000** on older firmware | Try `vizaio pair begin 192.168.1.50:9000` if the default fails |
| `vizaio: ... timeout` on `pair begin` | Device isn't in pairing mode (e.g., already paired and not reset), or it's powered off | Power-cycle the TV, or factory-reset SmartCast pairing in its menu |
| Device displays no PIN | Soundbars and Crave speakers don't display a PIN — they don't require auth at all. Use `vizaio device add` directly with `--device-type soundbar` (or `crave_*`) and skip pairing. | — |

### Library equivalent

If you'd rather pair from Python (e.g., a Home Assistant config flow), see
[Library API → Pairing](#pairing-1).

---

## CLI reference

The `vizaio` command is a thin wrapper over the library. Output adapts to
context: pretty tables when stdout is a TTY, TSV when piped, switchable to
JSON or plain.

### Global flags

| Flag | Meaning |
| --- | --- |
| `--device NAME` | Use a saved alias from the config file |
| `--host HOST` | Ad-hoc target (`IP` or `IP:PORT`); overrides `--device` |
| `--auth TOKEN` | Ad-hoc auth token; overrides any saved token |
| `--device-type tv\|soundbar\|crave_go\|crave360\|crave_pro` | Override the device family |
| `--config PATH` | Override the config file path; also via `$VIZAIO_CONFIG` |
| `--format table\|tsv\|json\|plain` | Force an output format (auto by default). Also accepted on each output-producing subcommand — leaf wins on conflict. |
| `-v`, `--verbose` | Enable debug logging to stderr |

Resolution order: `--host` > `--device` > `config.default_device`.

### Config file

Stored at `$VIZAIO_CONFIG` if set, otherwise
`platformdirs.user_config_dir("vizaio") / config.toml`
(macOS: `~/Library/Application Support/vizaio/config.toml`,
Linux: `~/.config/vizaio/config.toml`).

```toml
default_device = "livingroom"

[devices.livingroom]
host = "192.168.1.50"
device_type = "tv"
auth_token = "Zabc1234DEFG5678"

[devices.kitchen-bar]
host = "192.168.1.51"
device_type = "soundbar"
# soundbars don't require auth; field omitted
```

The file is created on first write with mode `0600` on Unix-like systems.

### `vizaio device` — manage saved aliases

```bash
# Save a TV with a token
vizaio device add livingroom --host 192.168.1.50 --device-type tv --auth Zabc1234

# Save a soundbar (no auth needed)
vizaio device add kitchen-bar --host 192.168.1.51 --device-type soundbar

vizaio device list
vizaio device show livingroom
vizaio device set-default livingroom
vizaio device remove kitchen-bar
```

After `set-default`, every other subcommand can drop `--device`:

```bash
vizaio power on              # acts on the default device
```

### `vizaio discover`

```bash
vizaio discover                          # zeroconf + SSDP, 5s timeout
vizaio discover --timeout 10 --no-ssdp   # zeroconf only, 10s
```

Exit code 1 if nothing was found.

### `vizaio pair`

Device pairing operations. Four subcommands:

```bash
# Begin a pairing session — outputs challenge data and a ready-to-run command
vizaio pair begin 192.168.1.50

# Complete pairing with the challenge data from "pair begin"
vizaio pair complete 192.168.1.50 \
    --challenge-type 1 --pairing-token 54321 --pin 1234

# Cancel an in-progress pairing session
vizaio pair cancel 192.168.1.50

# Interactive pairing — prompts for PIN
vizaio pair interactive 192.168.1.50
vizaio pair interactive 192.168.1.50 --save-as livingroom
```

`--device-type` defaults to `tv`. All subcommands accept `--device-id`
(defaults to `vizio-cli`); `begin`, `cancel`, and `interactive` also accept
`--device-name` (defaults to `vizaio CLI`) to match the identity
sent during pairing.

`pair begin` prints the `pair complete` command with all flags filled in
except `--pin`, making it trivially scriptable.

### `vizaio power`

```bash
vizaio power state           # 'on' or 'off'
vizaio power on
vizaio power off
```

### `vizaio volume`

```bash
vizaio volume level          # current value (0..max)
vizaio volume max            # the device's max-volume scale
vizaio volume up --steps 3
vizaio volume down
vizaio volume mute
vizaio volume unmute
```

### `vizaio input` (TV-only)

```bash
vizaio input list                         # name, meta_name, current
vizaio input current                      # just the name
vizaio input set HDMI-2
vizaio input next
```

### `vizaio remote`

```bash
vizaio remote keys                        # all valid key names for this device
vizaio remote send MENU
vizaio remote send GUIDE
vizaio remote send NUM_5
```

### `vizaio settings`

```bash
vizaio settings types                     # top-level categories
vizaio settings list audio                # all settings under 'audio'
vizaio settings get audio volume          # single value
vizaio settings set audio volume 30       # write (uses one-shot retry on race)
```

### `vizaio app` (TV-only)

```bash
vizaio app current                        # 'Netflix' or '(no app running)'
vizaio app launch Netflix
vizaio app launch-config 3 4              # raw app_id, name_space
vizaio app launch-config 3 4 'optional-message'
```

### `vizaio info`

```bash
vizaio info model
vizaio info serial
vizaio info esn
vizaio info version
vizaio info all                           # one row, all fields
```

### `vizaio battery` (Crave only)

```bash
vizaio battery level                      # integer
vizaio battery charging                   # 'not_charging' | 'charging' | 'fully_charged'
```

### Real-world combinations

```bash
# All HDMI inputs, names only
vizaio --device livingroom input list --format tsv | awk -F'\t' '/HDMI/ {print $1}'

# Audio settings as JSON for jq
vizaio settings list audio --format json | jq '.[] | select(.name=="volume") | .value'

# Set a default and then control without the flag
vizaio pair interactive 192.168.1.50 --save-as den
vizaio device set-default den
vizaio power on
vizaio volume up --steps 5
```

---

## Discovery

Programmatic discovery returns frozen `DiscoveredDevice` records — handy
when you're building a config flow or web UI on top of the library:

```python
import asyncio

from vizaio.discovery import (
    discover,
    discover_zeroconf,
    discover_ssdp,
)


async def main() -> None:
    # Unified: zeroconf + SSDP, deduped by IP, prefers zeroconf metadata
    everywhere = await discover(timeout=5.0)

    # Just zeroconf (matches what the official Vizio app does).
    # Requires the `[discovery]` extra; raises ImportError without it.
    only_mdns = await discover_zeroconf(timeout=5.0)

    # Just SSDP; works without the `[discovery]` extra.
    only_ssdp = await discover_ssdp(timeout=5.0)

    for d in everywhere:
        print(f"{d.name}  ip={d.ip}  port={d.port}  model={d.model}  id={d.id}")


asyncio.run(main())
```

Each `DiscoveredDevice` exposes `name`, `ip`, `port`, `model`, `id`, plus a
convenience `host` property combining `ip:port`. SSDP-discovered devices are
filtered to `manufacturer == "VIZIO"` to weed out Chromecasts and Rokus that
also speak DIAL.

### Host-targeted classification

When you already have a host (e.g., from a config-flow entry field) but don't
yet know its device type, use `async_classify_device` for full five-way
classification or `async_is_tv` for a binary TV-vs-audio result:

```python
from vizaio.discovery import async_classify_device, async_is_tv

# Full classification — returns one of the five DeviceType values
dt = await async_classify_device("192.168.1.50:7345")

# Binary probe — True = TV, False = audio device
is_tv = await async_is_tv("192.168.1.50")
```

Both functions are lenient: any probe failure (connection error, timeout,
unexpected response) returns `True` / `DeviceType.TV`. This matches HA
config-flow semantics — TV is the dominant case, and a wrong default is
corrected when the user confirms the device type.

If you already have a model string from `DiscoveredDevice.model` (returned by
the `discover_*` functions) or persisted config, you can classify without a
network round trip using the pure sync helpers. **Note:** `Vizio.get_model_name()`
is *not* a suitable source — for non-TV settings roots it returns the friendly
`NAME` field (e.g., `"Crave Go"`) rather than the canonical `SYSTEM_INFO.MODEL_NAME`
(`"SP30-E0"`) the SP-prefix matching needs.

```python
from vizaio.discovery import classify_crave_model, is_crave_model

if is_crave_model(model):          # True iff model.upper().startswith("SP")
    dt = classify_crave_model(model)
    # SP30* → CRAVE_GO, SP50* → CRAVE360, SP70* → CRAVE_PRO
    # Unknown SP* → CRAVE_GO (lenient default)
```

`classify_crave_model` raises `ValueError` if called on a non-Crave model;
always guard with `is_crave_model` first.

---

## Device-type quirks

| Capability | TV | Soundbar | Crave (Go / 360 / Pro) |
| --- | --- | --- | --- |
| Auth required | yes | no | no |
| `max_volume` | 100 | 31 | 24 |
| `vizaio input list` / `set` | yes | no | no |
| `vizaio app ...` | yes | no | no |
| `vizaio battery ...` | no | no | yes |
| WebSocket events | yes (probe-and-fall-back) | rejected by device | rejected by device |
| Remote keymap | full (channels, nav, numerics, …) | power, volume, mute, play/pause, input-next | same as soundbar |

A few non-obvious consequences:

- **Soundbar volume scales differently.** `vizaio settings set audio volume 1`
  on a soundbar is roughly **3% perceived volume** (1/31), not 1% (1/100).
  This is hardware behavior, not a bug; if you treat all SmartCast volume
  as a 0–100 scale you will surprise users with quiet soundbars. The
  library exposes `v.profile.max_volume` for normalization.
- **Crave variants share a profile family but are distinguished by hardware
  string** per APK findings: Go = `SP30-E0`, 360 = `SP50-D5`, Pro = `SP70-D5`.
  All three currently use the same capability flags and key map; the
  separate presets exist so per-model deltas (volume scale, key codes) can
  be added without breaking changes.
- **Apps and inputs raise `VizioUnsupportedError` synchronously** on
  unsupported profiles — the resolver checks profile capabilities before
  any HTTP call, so you never see a confusing 404 from the device. The
  CLI surfaces these as exit code 1 with a clear message.

---

## Library API

The CLI sits on top of an async Python library. Use it directly when you're
embedding control in another async program — Home Assistant integrations,
home-automation glue scripts, web services.

### Quickstart (already-paired device)

```python
import asyncio

from vizaio import DeviceType, Vizio


async def main() -> None:
    async with Vizio(
        host="192.168.1.50",            # placeholder — your device IP
        device_type=DeviceType.TV,
        auth_token="Zabc1234DEFG5678",  # placeholder — see "Pairing" above
    ) as v:
        await v.power_on()
        print("Volume:", await v.get_volume())
        for inp in await v.get_inputs():
            mark = "*" if inp.is_current else " "
            print(f"{mark} {inp.name}  ({inp.meta_name or '-'})")


asyncio.run(main())
```

Notes:

- `Vizio` is an async context manager. Use `async with` and the underlying
  `aiohttp` session is created on entry and closed on exit. If you need to
  share a session, pass `session=...` and the library will not close it for
  you.
- `device_type=DeviceType.TV` selects a built-in capability profile. Other
  presets: `SOUNDBAR`, `CRAVE_GO`, `CRAVE360`, `CRAVE_PRO`. To roll your own,
  pass `profile=DeviceProfile(...)` instead of `device_type=` (mutually
  exclusive).
- Soundbars and Crave speakers do not require an auth token; you can omit
  `auth_token=` for those.

### Pairing

Two ways to pair from Python:

**Stateful (context manager)** — the safety-net approach for interactive use:

```python
import asyncio

from vizaio import DeviceType, Vizio, VizioError


async def pair() -> None:
    async with Vizio(
        host="192.168.1.50",            # placeholder
        device_type=DeviceType.TV,
    ) as v:
        async with v.pair_session(
            device_id="my-app",         # stable identity for *your* client
            device_name="My App",       # human-readable; shows on the TV
        ) as session:
            # Device now displays a 4-digit PIN on screen.
            print("Look at the TV and enter the PIN it displays.")
            pin = input("PIN: ").strip()

            try:
                auth_token = await session.complete(pin=pin)
            except VizioError as e:
                # Context manager auto-cancels pairing on the way out.
                print(f"Pairing failed: {e}")
                return

        print("Save this token somewhere safe:")
        print(auth_token)


asyncio.run(pair())
```

The context manager is the safety net: if `complete()` is never reached —
for any reason, including `KeyboardInterrupt` — `__aexit__` issues a
best-effort `PUT /pairing/cancel` so the TV doesn't sit in pairing mode
until reboot.

**Stateless (one-shot methods)** — for scripted or multi-process pairing:

```python
from vizaio import DeviceType, PairChallenge, Vizio

async with Vizio(host="192.168.1.50", device_type=DeviceType.TV) as v:
    # Step 1: begin — device displays a PIN
    challenge: PairChallenge = await v.begin_pair(
        device_id="my-app", device_name="My App"
    )
    # challenge.challenge_type and challenge.token are needed for step 2

    # Step 2: complete — submit the PIN
    auth_token: str = await v.finish_pair(
        device_id="my-app",
        challenge_type=challenge.challenge_type,
        token=challenge.token,
        pin="1234",
    )

    # Optional: cancel a pairing session early (best-effort, swallows errors)
    await v.cancel_pair(device_id="my-app", device_name="My App")
```

These are the same primitives the CLI's `pair begin` / `pair complete` /
`pair cancel` subcommands use internally.

### Power

```python
state: bool = await v.get_power_state()
await v.power_on()
await v.power_off()
```

### Volume and mute

```python
level: int = await v.get_volume()       # raw value, 0..profile.max_volume
await v.volume_up(steps=3)              # send 3 KEYPRESSes in one PUT
await v.volume_down(steps=1)

muted: bool = await v.is_muted()
await v.mute()
await v.unmute()
```

`get_volume()` returns the device's raw value. The volume scale **differs by
device family** — see [Device-type quirks](#device-type-quirks). Divide by
`v.profile.max_volume` for a 0–1 range.

### Input switching

```python
inputs: list[InputInfo] = await v.get_inputs()
# InputInfo(cname='hdmi1', name='HDMI-1', meta_name='PS5', is_current=True)

current: str = await v.get_current_input()

# set_input accepts any of the four forms — case-insensitive — and
# translates internally to the cname (the device's canonical
# identifier in the PUT body):
await v.set_input("hdmi2")              # cname (most explicit)
await v.set_input("HDMI-2")             # display name
await v.set_input("Mac")                # user-renamed meta_name
await v.set_input("smartcast")          # works for the cast input too

await v.next_input()
```

`set_input` validates against the current input list and resolves to the
canonical cname before issuing the PUT. Unknown inputs raise
`VizioInvalidInputError` with the valid names listed; ambiguous matches
(e.g., the user renamed two HDMIs to `"Living Room"`) raise with the
candidates listed. If the target is already current, `set_input` is a
no-op (the device returns FAILURE for "switch to current input"; the
library treats this as success).

### App launching (TV-only)

```python
name: str | None = await v.get_current_app()
# 'Netflix' / 'YouTube' / None (no app running) / '_UNKNOWN_APP' (catalog miss)

await v.launch_app("Netflix")           # case-insensitive; matches catalog name

# Power-user: launch by raw config (skip catalog lookup)
from vizaio import AppConfig
await v.launch_app_config(AppConfig(app_id="3", name_space=4, message=None))
```

The catalog is fetched from Vizio's CDN with a 24h cache and falls back to a
copy bundled in the package if the CDN is unreachable. App support is
TV-only on current firmware; calling these on a soundbar raises
`VizioUnsupportedError` before any HTTP work.

### Settings (read and write)

```python
types: list[str] = await v.get_setting_types()
# ['audio', 'picture', 'system', 'network', ...]

audio: dict[str, SettingInfo] = await v.get_settings("audio")
# {'volume': SettingInfo(value=15, hashval=12345, type=SettingType.SLIDER, ...), ...}

vol: SettingInfo = await v.get_setting("audio", "volume")
# SettingInfo(setting_type='audio', name='volume', value=15, hashval=12345,
#             type=SettingType.SLIDER, min=0, max=100, options=())

# Simplest: do the GET-then-PUT for you, with one-shot retry on hashval race
await v.set_setting("audio", "volume", 30)

# Optimization: skip the GET if you already know the hashval
await v.set_setting("audio", "volume", 30, hashval=vol.hashval)
```

`HASHVAL` is a server-assigned, opaque integer that the device requires on
every write. The library handles the GET-then-PUT round trip and, if the
PUT fails with `invalid_parameter` (the hashval changed mid-flight, a
documented race in pyvizio issues #135 / #140), refetches and retries
**once** before raising. When you pass `hashval=` explicitly, no retry
fires — caller is in charge.

### Remote keys

```python
from vizaio import RemoteKey

await v.send_key("MENU")                # raw string
await v.send_key(RemoteKey.GUIDE)       # or the StrEnum

valid: frozenset[str] = v.available_keys
# {'POW_ON', 'POW_OFF', 'VOL_UP', 'VOL_DOWN', 'MUTE_ON', ..., 'MENU', 'GUIDE', ...}
```

The keymap differs by profile. Soundbars omit channel / navigation / numeric
keys; Crave speakers share the soundbar map. Calling `send_key("CH_UP")` on
a soundbar raises `VizioUnsupportedError` synchronously.

### Device identity

```python
model:  str        = await v.get_model_name()
serial: str        = await v.get_serial_number()
esn:    str        = await v.get_esn()
ver:    str        = await v.get_version()

info: DeviceInfo = await v.get_device_info()
# DeviceInfo(model='M65Q7-H1', serial_number='...', esn='...', version='...',
#            inputs=(InputInfo(...), ...))
```

`get_device_info()` aggregates four-or-five GETs and degrades each field to
`""` / `()` on individual failure — useful when one field is unsupported on
older firmware. Each individual getter still raises on failure if you call
it directly.

### Battery (Crave only)

```python
level:    int            = await v.get_battery_level()
charging: ChargingStatus = await v.get_charging_status()
# ChargingStatus.NOT_CHARGING / CHARGING / FULLY_CHARGED
```

Both raise `VizioUnsupportedError` at the resolver layer (no HTTP) on
non-Crave profiles.

### Health probes

```python
await v.ping()        # unauthenticated; cheapest "is the device reachable" check
await v.ping_auth()   # validates the configured token actually works
```

For a pre-construction *device-type probe* (before you have a `Vizio` instance),
use `async_is_tv` from `vizaio.discovery` — it doesn't require pairing. Note
that it is **not** a reachability check: lenient semantics mean an unreachable
host returns `True` (TV default). For genuine reachability, construct a `Vizio`
and call `ping()` — that surfaces transport failures as exceptions instead of
silently defaulting.

### App catalog injection

For integrations that manage their own refresh schedule (e.g., an HA apps
coordinator), fetch the catalog independently and push it into the `Vizio`
instance rather than letting the library auto-fetch:

```python
from vizaio import Vizio
from vizaio.apps import fetch_app_catalog, fetch_app_availability

# Fetch once, share across multiple Vizio instances or cache in coordinator
catalog = await fetch_app_catalog()                  # falls back to bundled on failure
availability = await fetch_app_availability()        # same fallback semantics

# Pass at construction time …
async with Vizio(host="...", auth_token="...", apps=catalog) as v:
    ...

# … or push fresh data post-construction (e.g., after coordinator refresh)
v.set_app_catalog(catalog)
v.set_app_availability(availability)
```

Each setter independently flips its own caller-owned flag. Calling
`set_app_catalog` skips auto-fetch for the catalog only; the availability
side keeps its own auto-fetch + TTL behavior unless you also call
`set_app_availability`. Same in reverse.

Pass `url=` to point at a regional mirror, internal proxy, or test fixture:

```python
catalog = await fetch_app_catalog(session=session, url="https://example.com/apps.json")
```

### Bulk state poll (`state_extended`)

For HA-style polling integrations, `get_state_extended()` returns power,
current input, current app, screen mode, and media state in **one** HTTP
round trip — meaningfully cheaper than five individual GETs. Available on
modern firmware that advertises `scpl_capabilities.state_extended` (most
TVs from ~2022 onward).

```python
s = await v.get_state_extended()
# StateExtended(power_on=True, power_mode='Quick Start',
#               current_input='SMARTCAST',
#               current_input_hashval=3009117460,
#               current_app=AppConfig(app_id='1', name_space=4, ...),
#               screen_mode='Full screen',
#               media_state='MediaState::Stopped',
#               device_name='Test TV', errors=())
```

Older firmware that doesn't expose the endpoint raises
`VizioNotFoundError` (URI_NOT_FOUND); fall back to individual getters.

### Event subscription (WebSocket)

For supported TVs, the library can subscribe to push events instead of
polling. After `PUT /event/register`, the device opens a WebSocket and
streams JSON envelopes whenever a tracked property changes.

```python
async with Vizio(host="192.168.1.50", auth_token="...") as v:
    async with v.subscribe_events() as events:
        async for ev in events:
            # StateEvent(uri='audio/volume/level', value=15, cname='volume',
            #            hashval=12345, raw={...})
            print(ev.uri, ev.value)
```

Per APK reverse-engineering the device explicitly demultiplexes five URIs:
`state/device/power_mode`, `app/current`, `system/context_change`,
`audio/volume/level`, `audio/volume/mute`. Other URIs may also fire — the
library does not filter, so unknown URIs reach your iterator with
`value=None` and the full envelope on `ev.raw`.

`subscribe_events(auto_reconnect=True, reconnect_delay=15.0)` reconnects
silently after a drop. Pass `auto_reconnect=False` to end the iterator on
the first disconnect. The Android app gates this feature on TV-only and
non-Marvell-SoC chipsets; this library does not — it probes by attempting
`PUT /event/register` and surfaces errors. Soundbars typically reject the
register and will raise.

### Custom device profiles

When Vizio ships a new soundbar variant or you want to override
`max_volume` for a quirky firmware, build a custom profile:

```python
from vizaio import DeviceProfile, Vizio
from vizaio.endpoints import SettingsRoot
from vizaio._keys import SOUNDBAR_KEYS

custom = DeviceProfile(
    name="My Vizio Frankenbar",
    settings_root=SettingsRoot.AUDIO,
    max_volume=50,
    requires_auth=False,
    has_battery=False,
    has_inputs=False,
    has_apps=False,
    keymap=SOUNDBAR_KEYS,
)

async with Vizio(host="192.168.1.99", profile=custom) as v:
    ...
```

`device_type` and `profile` are mutually exclusive on the constructor.

---

## Exception hierarchy

All exceptions raised by `vizaio` derive from `VizioError`. Catch the
base class for "any device problem", or a specific subclass when you want to
distinguish (e.g., a Home Assistant config flow that handles auth failures
differently from connection failures).

```text
VizioError
├── VizioConnectionError       # transport-level: TCP/TLS/timeout/HTTP non-200
├── VizioAuthError             # auth missing, invalid, or rejected
├── VizioResponseError         # malformed JSON, unknown STATUS.RESULT
├── VizioInvalidParameterError # device returned RESULT=invalid_parameter
│   └── VizioInvalidInputError # `set_input` called with an unknown input name
├── VizioNotFoundError         # response missing the expected ITEM cname
├── VizioBusyError             # device returned RESULT=blocked
└── VizioUnsupportedError      # operation not supported by this device profile
```

Quick guide to when each is raised:

| Exception | Raised when |
| --- | --- |
| `VizioConnectionError` | TCP can't reach the device, TLS handshake fails, the request times out, or the device returns a non-200 HTTP status |
| `VizioAuthError` | An endpoint requires auth and no token was configured, or the device returns `RESULT=requires_pairing` / `pairing_denied` |
| `VizioResponseError` | The body parses but its shape is unexpected, or `RESULT` is `failure` / unknown |
| `VizioInvalidParameterError` | The device returns `RESULT=invalid_parameter` — most often a stale hashval after `set_setting`'s second attempt also lost the race, or genuinely invalid input to `launch_app_config` |
| `VizioInvalidInputError` | (subclass of the above) `set_input` was given a name not present in `get_inputs()` |
| `VizioNotFoundError` | The HTTP call succeeded but the response is missing the `ITEMS` entry the call expected — used internally as the fallthrough trigger when an endpoint has multiple firmware-version paths |
| `VizioBusyError` | The device returns `RESULT=blocked` (e.g., another client holds a write lock, or the device is mid-update) |
| `VizioUnsupportedError` | Operation can't be done on this device profile (e.g., `get_battery_level` on a TV, `launch_app` on a soundbar). Raised before any HTTP request is sent |

The CLI surfaces these as exit codes:

- `0` — success
- `1` — any `VizioError` subclass (the message is printed to stderr)
- `2` — CLI resolution error (no device specified, unknown alias, etc.)

---

## License

MIT. See [`LICENSE`](LICENSE).
