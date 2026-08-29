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
# library only (REST client; just aiohttp underneath)
uv add vizaio
# or: pip install vizaio

# library + the `vizaio` CLI
uv add 'vizaio[cli]'
# or: pip install 'vizaio[cli]'

# + zeroconf network discovery (optional, combinable: 'vizaio[cli,discovery]')
uv add 'vizaio[discovery]'
# or: pip install 'vizaio[discovery]'
```

The base install is deliberately dependency-light (`aiohttp` only) so
library consumers — e.g. the Home Assistant integration, whose CI resolves
every integration's requirements together — don't inherit the CLI's
dependencies (`typer`, `rich`, `tomlkit`, `platformdirs`). Running the
`vizaio` command without the `[cli]` extra prints the install hint instead
of a traceback.

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

The `vizaio` console script is always installed as a project entry point;
it needs the `[cli]` extra (included by `--all-extras` above) to actually
run.

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
vizaio --device kitchen-bar mute on
```

---

## Pairing

Pairing is the same HTTP handshake on every device family — TVs, soundbars,
and Crave speakers all use `PUT /pairing/start`, `/pairing/pair`,
`/pairing/cancel`.

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

### `vizaio probe`

Classify a host as one of the five device types without pairing or any saved
alias — useful when you don't yet know the device family.

A port is required — vizaio doesn't assume a default. TVs typically listen
on 7345, audio devices on 9000 (verify via `vizaio discover` first).

```bash
vizaio probe 192.168.1.50:7345                  # port embedded in host
vizaio probe 192.168.1.50 --port 7345           # port as a separate flag
vizaio probe 192.168.1.50:7345 --format json    # machine-readable output
```

Output columns: `host`, `device_type`. `device_type` is one of `tv`,
`soundbar`, `crave_go`, `crave360`, `crave_pro`.

Classification is lenient: an unreachable host returns `tv` (the dominant
case for Home Assistant users); a reachable audio device whose model info
cannot be fetched returns `soundbar`.

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

### `vizaio wifi`

Hands Wi-Fi credentials to a device sitting on its own setup access point —
what the SmartCast app's onboarding does. Join the device's hotspot first;
its address is your default gateway on that network.

```bash
# See what the device can see
vizaio wifi scan 192.168.1.1

# Hand over credentials directly
vizaio wifi join 192.168.1.1 MyNetwork --password hunter2
vizaio wifi join 192.168.1.1 GhostNet --password hunter2 --hidden

# Guided: scan, pick from a numbered list, prompt for the password
vizaio wifi interactive 192.168.1.1
```

Like `vizaio pair`, the host is positional rather than a saved alias — a
device in setup mode has none. A bare IP probes ports 7345 and 9000, so you
do not need to know that soundbars answer on 9000. `--device-type` defaults
to `soundbar`.

None of these confirm the device joined: on success it leaves its hotspot,
so your host loses its route to it. Rejoin your normal network and run
`vizaio discover`.

### `vizaio power`

```bash
vizaio power state           # 'on' or 'off'
vizaio power on
vizaio power off
vizaio power toggle          # press the power button — flips whatever state
```

### `vizaio mute`

```bash
vizaio mute on               # idempotent — reads state, no-op if already muted
vizaio mute off              # idempotent — reads state, no-op if already unmuted
vizaio mute toggle           # one round trip — presses MUTE_TOGGLE blindly
```

### `vizaio screen`

```bash
vizaio screen blank          # panel off, audio keeps playing (hold-mute "mute screen"; TV only)
```

Wake the panel with any navigation key, e.g. `vizaio remote send BACK`
(volume/mute keys intentionally don't wake it, so sound stays adjustable
while the screen is dark). There is no readable "blanked" state.

### `vizaio volume`

```bash
vizaio volume level          # current value (0..max)
vizaio volume set 30         # absolute set (flat endpoint, no hashval, 1 round trip)
vizaio volume max            # the device's max-volume scale
vizaio volume up --steps 3
vizaio volume down
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
vizaio remote text "stranger things"      # type ASCII into a search/keyboard field
```

### `vizaio settings`

```bash
vizaio settings types                     # top-level categories
vizaio settings list audio                # all settings under 'audio'
vizaio settings get audio volume          # single value
vizaio settings set audio volume 30       # write (uses one-shot retry on race)
vizaio settings action system/timers blank_screen   # fire a T_ACTION_V1 item
```

`settings action` triggers action-type menu items (`T_ACTION_V1`) — things
like `system/timers blank_screen` (Blank Screen) or `admin_and_privacy
soft_power_cycle` (reboot). These use the `REQUEST: "ACTION"` verb, not
`MODIFY`; see `docs/protocol-notes.md` #29.

### `vizaio pin`

```bash
vizaio pin status            # is the parental/purchase PIN still factory-default? (true/false)
```

Read-only. vizaio does not expose PIN *setters* (`set_pin`/`confirm_pin` are a
lockout-risky write with under-specified semantics) or `pairing/unpair` (a route
constant the official app never actually calls).

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

### `vizaio versions`

```bash
vizaio versions              # firmware, serial, ESN + per-component versions (SCPL, ACR, …)
vizaio versions --format json
```

One `GET /system/versions` round trip — a cleaner source for firmware /
serial / ESN than the per-field `vizaio info` scrapes, and a good companion
to `get_state_extended()` for one-shot device snapshots.

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

The two functions have **different failure contracts**:

- `async_is_tv` is **lenient** — any probe failure (connection error, timeout,
  unexpected response) returns `True` (TV). This matches HA config-flow
  semantics where TV is the dominant case and a wrong default is corrected
  when the user confirms the device type. Use this when you want a binary
  answer no matter what.
- `async_classify_device` is **strict** — issues a single unauthenticated
  GET to `/state/device/deviceinfo` and raises `VizioConnectionError` if
  the host is unreachable, or `VizioResponseError` if the response is
  malformed. The function's job is to classify; if it can't, raising is
  more honest than returning a lenient default. Wrap in `try`/`except`
  (or use `async_is_tv` instead) if you want to handle failure as TV.

### Resolving a host that has no port

A bare IP has no port, and the client builds URLs literally — so
`Vizio("192.168.1.50")` targets port 443 and fails. When a host comes from
user input or persisted config and may lack a port, run it through
`async_resolve_host` first:

```python
from vizaio.discovery import async_resolve_host

host = await async_resolve_host("192.168.1.50")  # → "192.168.1.50:7345"
already = await async_resolve_host("192.168.1.50:9000")  # unchanged, no I/O
```

It probes `DEFAULT_PORTS` (`7345`, then `9000`) and raises
`VizioConnectionError` if none respond. Each candidate is validated with a
real unauthenticated `deviceinfo` fetch rather than a bare TCP connect —
Vizio devices also listen on Chromecast/DIAL ports (7000/8008/8009/8443) that
accept connections but don't speak the SmartCast API, so an open socket alone
proves nothing. Pass `ports=` to override the candidates.

If you already have a model string from `DiscoveredDevice.model` (returned by
the `discover_*` functions) or persisted config, you can classify without a
network round trip using the pure sync helpers. **Note:** `Vizio.get_model_name()`
is *not* a suitable source — for non-TV settings roots it returns the friendly
`NAME` field (e.g., `"Crave Go"`) rather than the canonical `SYSTEM_INFO.MODEL_NAME`
(`"SP30-E0"`) the SP-prefix matching needs.

```python
from vizaio.discovery import classify_crave_model, is_crave_model

if is_crave_model(model):  # True iff model.upper().startswith("SP")
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
        host="192.168.1.50",  # placeholder — your device IP
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
        host="192.168.1.50",  # placeholder
        device_type=DeviceType.TV,
    ) as v:
        async with v.pair_session(
            device_id="my-app",  # stable identity for *your* client
            device_name="My App",  # human-readable; shows on the TV
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

    # Step 2: complete — pass the challenge back along with the PIN
    auth_token: str = await v.finish_pair(
        device_id="my-app", challenge=challenge, pin="1234"
    )

    # Optional: cancel a pairing session early (best-effort, swallows errors)
    await v.cancel_pair(device_id="my-app", device_name="My App")
```

These are the same primitives the CLI's `pair begin` / `pair complete` /
`pair cancel` subcommands use internally.

### Provisioning a soundbar's Wi-Fi

A factory-fresh soundbar broadcasts an open setup hotspot. Join it first —
that is an OS-level step this library does not perform — then point `vizaio`
at your default gateway, which is the device's address on its own network.

```python
from vizaio import DeviceType, Vizio

async with Vizio(host="192.168.1.1:9000", device_type=DeviceType.SOUNDBAR) as v:
    async with v.wifi_setup_session() as session:
        for ap in await session.access_points():
            print(ap.ssid, ap.band, "open" if ap.is_open else ap.security)
        await session.join("MyNetwork", password="hunter2")
```

The session starts a scan on entry and always stops it on exit. Call
`access_points()` again to refresh — scans fill in over several seconds.

Or interactively:

```console
vizaio wifi interactive 192.168.1.1
```

Neither confirms the device joined: it leaves its hotspot on success, so the
host loses its route to it. Rejoin your normal network and run
`vizaio discover`.

### Power

```python
state: bool = await v.get_power_state()
await v.power_on()
await v.power_off()
await v.power_toggle()  # press the power button (flips state)
```

### Volume and mute

```python
level: int = await v.get_volume()  # raw value, 0..profile.max_volume
await v.set_volume(30)  # absolute set; flat /audio/volume/level,
# no hashval, one round trip
await v.volume_up(steps=3)  # send 3 KEYPRESSes in one PUT
await v.volume_down(steps=1)

muted: bool = await v.is_muted()
await v.mute()  # idempotent
await v.unmute()  # idempotent
await v.mute_toggle()  # one round trip: press MUTE_TOGGLE blindly
```

`mute()` and `unmute()` are idempotent. On volume-V2 firmware (below) they
send the desired state in a single `PUT` — which is also **race-free**.
Elsewhere they read the current state and send `MUTE_TOGGLE` only on mismatch,
which can lose to someone pressing mute on the physical remote in between.
`mute_toggle()` is always one round trip and matches the remote button.

`get_volume()` returns the device's raw value, read from the `audio/volume`
**leaf** — never the `audio` collection, which on some firmware omits
`volume` entirely (see below). The volume scale **differs by device family**
— see [Device-type quirks](#device-type-quirks):

```python
max_volume: int = await v.get_max_volume()  # asks the device, cached
fraction = await v.get_volume() / max_volume
```

`get_max_volume()` reads `MAXIMUM` from the device's static settings tree and
falls back to `v.profile.max_volume` when the device doesn't expose it.
`set_volume()`'s range check deliberately stays on the profile constant so a
setter never hides a round trip in argument validation.

#### Volume API V2

Newer TV firmware adds a flat `/audio/volume/*` family. `vizaio` detects it
the same way the official app does — from the device's `API_VERSION`, not
from any capability flag:

```python
await v.get_api_version()  # e.g. "3.3.3-2538.0001" (protocol spec, not firmware build)
await v.supports_volume_v2()  # True on a TV at >= 2.0.0-2031.0014
```

Both detectors are cheap: they read the deviceinfo payload the client already
caches, and an audio-root profile (soundbar, Crave) answers `False` with no
network call at all.

**There is no knob for any of this** — the library picks the path.
`set_volume()`, `mute()` and `unmute()` use the flat endpoints automatically
when the gate opens, because both are hardware-verified here and each is a clear
win (1 round trip vs 3 for each, and mute becomes race-free).

`volume_up()`/`volume_down()` always send keypresses, gate or no gate. A
keypress batch is already a single `PUT` for any realistic step count, it works
on every device family, and it is the only path Vizio's own app kept — recent
builds dropped the flat volume endpoints entirely. So `increase`/`decrease`
would never be the better choice, and offering them as an option would just
push a decision onto you that the library can already answer.

`supports_volume_v2()` and `get_api_version()` are exposed for inspection and
diagnostics, not because you need them to get the right behaviour.

> **Heads-up for bulk readers.** Some firmware omits `volume` (and `mute`)
> from `get_settings("audio")` while the individual leaves still work, and
> there is no client-observable way to predict which. Don't index that dict
> for volume — use `get_volume()` / `is_muted()`. This is the root cause of
> [home-assistant/core#179254](https://github.com/home-assistant/core/issues/179254);
> see protocol-notes #30 and #31.

### Input switching

```python
inputs: list[InputInfo] = await v.get_inputs()
# InputInfo(cname='hdmi1', name='HDMI-1', meta_name='PS5', is_current=True)

current: str = await v.get_current_input()

# set_input accepts any of the four forms — case-insensitive — and
# translates internally to the cname (the device's canonical
# identifier in the PUT body):
await v.set_input("hdmi2")  # cname (most explicit)
await v.set_input("HDMI-2")  # display name
await v.set_input("Mac")  # user-renamed meta_name
await v.set_input("smartcast")  # works for the cast input too

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

await v.launch_app("Netflix")  # case-insensitive; matches catalog name

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

```python
# Action items (T_ACTION_V1) fire, they don't hold values:
await v.blank_screen()  # panel off, audio keeps playing
await v.send_key("BACK")  # any nav key wakes the panel
await v.trigger_setting_action("admin_and_privacy", "soft_power_cycle")  # reboot
```

### Remote keys

```python
from vizaio import RemoteKey

await v.send_key("MENU")  # raw string
await v.send_key(RemoteKey.GUIDE)  # or the StrEnum

await v.send_text("stranger things")  # type ASCII into a search / keyboard field

valid: frozenset[str] = v.available_keys
# {'POW_ON', 'POW_OFF', 'VOL_UP', 'VOL_DOWN', 'MUTE_ON', ..., 'MENU', 'GUIDE', ...}
```

The keymap differs by profile. Soundbars omit channel / navigation / numeric
keys; Crave speakers share the soundbar map. Calling `send_key("CH_UP")` on
a soundbar raises `VizioUnsupportedError` synchronously.

`send_text()` sends each character as its ASCII code point (`CODESET 0`,
`CODE = ord(c)`) in one batched `KEYLIST` — the official app's on-screen
keyboard encoding. Non-ASCII raises `VizioInvalidInputError`. This is also the
reliable way to enter digits (`'0'`–`'9'` = ASCII 48–57; the `RemoteKey.NUM_*`
codeset-0 codes 0–9 are ASCII control characters, not digits).

### Device identity

```python
model: str = await v.get_model_name()
serial: str = await v.get_serial_number()
esn: str = await v.get_esn()
ver: str = await v.get_version()

info: DeviceInfo = await v.get_device_info()
# DeviceInfo(model='M65Q7-H1', serial_number='...', esn='...', version='...',
#            inputs=(InputInfo(...), ...))
```

`get_device_info()` aggregates four-or-five GETs and degrades each field to
`""` / `()` on individual failure — useful when one field is unsupported on
older firmware. Each individual getter still raises on failure if you call
it directly.

```python
versions: SystemVersions = await v.get_versions()  # GET /system/versions
# SystemVersions(firmware='3.720.9.1-1', serial_number='...', esn='...',
#                scpl='3.4.3-2614.0002', raw={'FIRMWARE': ..., 'acr': ..., ...})
versions.raw["SC CONFIG"]  # full device-cased map
```

`get_versions()` returns firmware / serial / ESN plus the per-component
version map (SCPL, ACR, AppleTV, …) in **one** round trip — a cleaner source
than the per-field identity getters above, and a natural companion to
`get_state_extended()` for one-shot device snapshots.

```python
default: bool = await v.is_pin_default()  # GET /pin/is_pin_default
# True  -> parental/purchase PIN is still factory-default (unprotected)
# False -> a custom PIN has been set
```

A read-only status check. vizaio intentionally omits PIN *setters* and
`pairing/unpair` (see the [`vizaio pin`](#vizaio-pin) CLI note).

### Battery (Crave only)

```python
level: int = await v.get_battery_level()
charging: ChargingStatus = await v.get_charging_status()
# ChargingStatus.NOT_CHARGING / CHARGING / FULLY_CHARGED
```

Both raise `VizioUnsupportedError` at the resolver layer (no HTTP) on
non-Crave profiles.

### Health probes

```python
await v.ping()  # unauthenticated; cheapest "is the device reachable" check
await v.ping_auth()  # validates the configured token actually works
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
catalog = await fetch_app_catalog()  # falls back to bundled on failure
availability = await fetch_app_availability()  # same fallback semantics

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

> **Note on push events:** vizaio is a REST control-plane client and does
> not implement WebSocket/push state updates. On real hardware the SmartCast
> event socket turned out to be part of the device's **Google Cast** control
> plane (a `ws://<ip>:8005` socket the app only opens for Cast-discovered
> devices), not a general mDNS/REST feature. For push/real-time updates,
> discover the TV through its Chromecast-built-in interface (e.g. Home
> Assistant's Google Cast integration) and poll vizaio for SmartCast state.

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
