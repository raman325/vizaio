# Soundbar Wi-Fi Provisioning — Design

**Issue:** [#40](https://github.com/raman325/vizaio/issues/40) — "Ability to pair
soundbars to wifi networks"

**Date:** 2026-08-28

## Problem

A Vizio soundbar that has never been on a network (factory-fresh, or after a
network reset) broadcasts an open access point and serves the ordinary
SmartCast REST API on it. The only supported way to hand it Wi-Fi credentials
is the SmartCast phone app, which users report as increasingly unreliable.
`vizaio` already speaks every protocol primitive the flow needs, but exposes no
API that makes the sequence usable.

## Evidence

Two sources, both recorded in `docs/protocol-notes.md` §32.

**Decompile** (`com.vizio.vue.launcher` 5.0.0, the last build before R8 name
obfuscation): `com/vizio/smartcast/onboarding/VizioDeviceWifiSetup.java` is the
first-time-setup flow and
`com/vizio/smartcast/menutree/ui/viewmodel/AccessPointsViewModel.java` is the
change-network flow. Both drive the same leaves.

**Hardware** (issue #40, reported by @micahlt on a real soundbar in setup mode):
the full sequence executed successfully. Three facts came only from that run and
could not have been inferred from the decompile:

- The device answers on **port 9000**, not 7345.
- **No `AUTH` header is required** — every request succeeded unauthenticated.
- `current_access_point` accepts **`NAME` alone**; the `NAME` + `PASSWORD`
  variant that `AccessPointsViewModel` sends did *not* work on this firmware.

Unverified: the hidden-network path, and every `NET_*` failure code (the
hardware run returned `SUCCESS` at every step).

## Scope

**In:** SDK primitives, a `WifiSetupSession` context manager, a `vizaio wifi`
CLI command group including an interactive wizard, the hidden-network path
(flagged unverified), typed Wi-Fi errors.

**Out:**

- **Joining the soundbar's access point.** This is an OS-level operation. The
  official app delegates it to Android's `WifiManager`; its own protocol-layer
  `SoftApClient` is a stub that throws `NotImplementedError` with the comment
  "Use WifiManager.enable(ssid) to make connection". The caller joins the
  hotspot; `vizaio` takes over from there.
- **Post-provision verification.** Once credentials land, the soundbar leaves
  its own AP and the host loses its route to it. Polling
  `test_connection_results` across a link that is disappearing would fail
  spuriously. The honest check is re-discovering the device on the real LAN,
  which `vizaio discover` already does. The `test_connection` leaves are
  documented in protocol-notes but get no API.
- `provision_wifi(ssid, password)` one-shot sugar. It cannot express "show me
  what's out there", which is the whole point of the interactive flow. Could be
  layered on later.

## Architecture

The design mirrors pairing, which already establishes the pattern in this
codebase: public primitives on `Vizio` (`begin_pair` / `finish_pair` /
`cancel_pair`) **plus** an ergonomic context manager (`pair_session`) written in
terms of them. Provisioning gets the same two layers for the same reasons —
nothing new for a reader to learn, and no forced funnel for callers whose flow
does not fit the session.

### `types.py`

```python
@dataclass(frozen=True, slots=True)
class AccessPoint:
    ssid: str  # NAME
    bssid: str  # BSSID
    security: str  # EM, e.g. "WPA2/PSK", "NONE"
    band: str  # BAND, "2.4" | "5"
    rssi: int  # RSSI, 0-100 as the device scales it

    @property
    def is_open(self) -> bool: ...
```

`is_open` ports `VZAccessPointItem.isSecure()` exactly: the network is secured
iff `EM` contains one of `WEP`, `PSK`, `EAP`, `WPA`, `WPA2` **and** is not the
literal `WEP/NONE`. Both `NONE` and `WEP/NONE` therefore read as open.

```python
class WifiResult(StrEnum):
    ALREADY_CONNECTED = "net_wifi_already_connected"
    NEEDS_VALID_SSID = "net_wifi_needs_valid_ssid"
    MISSING_PASSWORD = "net_wifi_missing_password"
    AUTH_REJECTED = "net_wifi_auth_rejected"
    NOT_FOUND = "net_wifi_not_existed"
    CONNECT_TIMEOUT = "net_wifi_connect_timeout"
    CONNECT_ABORTED = "net_wifi_connect_aborted"
    CONNECT_ERROR = "net_wifi_connect_error"
    CONNECTION_ERROR = "net_wifi_connection_error"
    DHCP_FAILED = "net_ip_dhcp_failed"
    MANUAL_CONFIG_ERROR = "net_ip_manual_config_error"
    UNKNOWN_ERROR = "net_unknown_error"
    UNKNOWN = "unknown"
```

`UNKNOWN` preserves the raw device string, following the existing
`ResponseStatus.UNKNOWN` convention. These are a distinct family from
`ResponseStatus` — radio and DHCP outcomes rather than protocol outcomes — so
they get their own enum rather than diluting that one.

`REQUIRES_SYSTEM_PIN` is deliberately *not* in this enum. In the app's constants
it carries the `RESPONSE_` prefix and sits beside `REQUIRES_PAIRING`, i.e. it is
a protocol status, not a radio outcome. It is added to `ResponseStatus` instead
and mapped to `VizioAuthError`, which is where a caller would already look for
"the device refused this until you authenticate something".

Additionally, the setting types this subtree reports are added to `SettingType`:
`T_APS_V1`, `T_AP_V1`, `T_STRING_V1`, `T_TEST_CONNECTION_V1`. They currently
fall through to `MENU`, which misreports what `get_settings("network")` returns.
Small, adjacent, worth fixing here.

### `endpoints.py`

Six rows, all under `{root}/network/`:

| Endpoint member | Path | Method |
| --- | --- | --- |
| `AP_SCAN_START` | `{root}/network/start_ap_search` | PUT |
| `AP_SCAN_STOP` | `{root}/network/stop_ap_search` | PUT |
| `ACCESS_POINTS` | `{root}/network/wireless_access_points` | GET |
| `CURRENT_ACCESS_POINT` | `{root}/network/current_access_point` | GET |
| `WIFI_PASSWORD` | `{root}/network/set_wifi_password` | GET |
| `HIDDEN_NETWORK` | `{root}/network/hidden_network` | GET |

Each row declares a single method, as every other row does. The three
write targets are declared `GET` because the write is always preceded by a
hashval fetch on the same path; the PUT reuses the existing
`replace(spec, method="PUT")` step that `_put_settings_body` already performs.
Named rows for settings-tree leaves follow the precedent set by
`Endpoint.INPUTS` and `Endpoint.CURRENT_INPUT`, which are likewise
`{root}/...` paths.

Auth is `AuthRequirement.PROFILE`, not `NONE`. The hardware run needed no auth,
but that follows from the soundbar profile's `requires_auth=False` rather than
from the endpoints being unauthenticated in general — the same leaves are what a
configured TV uses to change networks, and that path authenticates. `PROFILE`
gets both cases right without special-casing.

Setup uses the pre-2020 path spellings. `buildOOBEMenuTreeEndpoint()` hardcodes
`URIYearOptions.YEAR_PRE_2020`, so the 2020 renames (`wifi_networks`,
`wifi_password_entry`) apply only to the in-app change-network path and are not
implemented.

### `_payloads.py`

```python
def select_access_point(*, ssid: str, hashval: int) -> dict[str, Any]:
    return {"REQUEST": "MODIFY", "VALUE": [{"NAME": ssid}], "HASHVAL": hashval}


def join_hidden_network(*, ssid: str, password: str, hashval: int) -> dict[str, Any]:
    return {
        "REQUEST": "MODIFY",
        "VALUE": [{"NAME": ssid, "PASSWORD": password}],
        "HASHVAL": hashval,
    }
```

The password step reuses the existing `write_setting(value=password,
hashval=...)`. Scan start/stop reuse the existing `action_setting(hashval=...)`.

### `parse.py`

```python
def parse_access_points(response: Response) -> tuple[AccessPoint, ...]
def parse_current_access_point(response: Response) -> AccessPoint | None
```

`parse_current_access_point` returns `None` for the unconfigured sentinel the
hardware run captured — `NAME: ""` with `BSSID: "000000-000000"`. That rule is
only knowable from the capture.

### `_device.py`

```python
async def start_ap_scan(self) -> None
async def stop_ap_scan(self) -> None
async def get_access_points(self) -> tuple[AccessPoint, ...]
async def get_current_access_point(self) -> AccessPoint | None
async def join_access_point(
    self, ssid: str, *, password: str | None = None, hidden: bool = False
) -> None
def wifi_setup_session(self) -> WifiSetupSession
```

`start_ap_scan` and `stop_ap_scan` are `T_ACTION_V1` fires, byte-identical to
what `trigger_setting_action("network", ...)` already emits — confirmed against
the capture. They get named methods for discoverability, not because they need
new machinery.

`join_access_point` is the only branching method:

- `hidden=True` → one PUT to `HIDDEN_NETWORK` with `NAME` + `PASSWORD`.
- otherwise → PUT `CURRENT_ACCESS_POINT` with `NAME` alone (the verified shape),
  then PUT `WIFI_PASSWORD` with the password.

`password=None` sends `""`, matching the app, which always performs the password
step even for open networks. Both PUTs use the existing GET-for-hashval plus
single-retry-on-`INVALID_PARAMETER` path (protocol-notes §13), so they behave
like every other write in the library.

### `WifiSetupSession`

```python
async with Vizio(host="192.168.1.1:9000", device_type=DeviceType.SOUNDBAR) as v:
    async with v.wifi_setup_session() as session:
        for ap in await session.access_points():
            print(ap.ssid, ap.security, ap.rssi)
        await session.join("MinasTirith", password="...")
```

- `__aenter__` fires `start_ap_scan`, and best-effort stops it if that call
  itself raises (same defensive shape as `PairSession.__aenter__`).
- `__aexit__` **always** stops the scan, best-effort, swallowing `VizioError` so
  a failed cleanup cannot mask the caller's exception.
- `access_points()` performs one GET and returns the current list. Callable
  repeatedly to refresh. No internal polling and no invented timeout — the
  caller decides when it has waited long enough.
- `join()` delegates to `join_access_point` and is re-callable, so a mistyped
  password can be retried inside the same session.

The one divergence from `PairSession`: there is no `_completed` flag. Pairing
needs one because cancelling a completed pairing would undo it. Here the app
stops the scan on the happy path too (`stopDeviceWifiScan` runs immediately
after the password step), so stopping unconditionally on exit is correct for
both success and abort.

### CLI — `vizaio wifi`

```
vizaio wifi scan HOST
vizaio wifi join HOST SSID [--password TEXT] [--hidden]
vizaio wifi interactive HOST
```

`HOST` is positional rather than using the global `--device`/`--host`
resolution, for the same reason `vizaio pair` does it: a soundbar in setup mode
has no saved alias. Bare IPs are resolved through
`discovery.async_resolve_host`, which probes `DEFAULT_PORTS` `(7345, 9000)`, so
the user never needs to know their soundbar answers on 9000.
`--device-type` defaults to `soundbar` here rather than `tv`.

The `interactive` wizard:

1. Resolve the host, reporting which port answered. Open the session; the scan
   starts.
2. Read the AP list. If empty, prompt "No networks found yet — scan again?
   [Y/n]" and re-read.
3. Print a numbered table (#, SSID, band, security, signal) plus a
   `0) Hidden network…` entry.
4. Prompt for a number. `0` prompts for an SSID and routes to the hidden path.
5. Prompt for the password with `hide_input=True`, **skipped entirely** when
   `ap.is_open`.
6. `session.join(...)`. On `VizioWifiError` with `result` of `AUTH_REJECTED` or
   `MISSING_PASSWORD`, re-prompt for the password rather than aborting.
7. On exit the scan stops. Print the handoff: "Credentials sent. Rejoin your
   normal Wi-Fi, then run `vizaio discover` to find the soundbar on your
   network."

Human-facing progress goes to stderr, matching `pair interactive`. `wifi scan`
honors `--output-format`.

## Error handling

Two behavioral rules taken from the app rather than invented:

- **`NET_WIFI_ALREADY_CONNECTED` is not an error.** `VizioDeviceWifiSetup`
  treats it as success and proceeds to stop the scan. `join_access_point`
  returns normally on it.
- **`NET_WIFI_NEEDS_VALID_SSID` after the password step raises.** This is a
  deliberate divergence: the app's success predicate is `isSuccessful() ||
  isWifiNeedsValidSsid()`, which appears to be a workaround for its own UI
  ordering rather than a protocol truth. Swallowing "no valid SSID" would make a
  failed provision report success. Documented as a known departure from the
  reference implementation.

Every other `NET_*` code raises `VizioWifiError` carrying both the parsed
`WifiResult` and the raw device string. The typed error exists to let the CLI
wizard re-prompt on an auth rejection instead of dumping a traceback; without
the discriminator it would have to string-match.

## Testing

**Captured fixtures.** The hardware run produced real device JSON, and
`tests/captured/` exists for exactly this. Adding, with SSIDs and BSSIDs
redacted as they already are in the issue thread:

- `network_start_ap_search.json` (GET and PUT)
- `network_wireless_access_points.json` (3 APs, mixed 2.4/5 GHz, all WPA2/PSK)
- `network_current_access_point_unset.json` (the `NAME: ""` sentinel)
- `network_set_wifi_password.json` (GET and PUT)
- `network_stop_ap_search.json` (GET and PUT)

Every existing fixture in that directory comes from one VHD24M-0810 TV. These
are the suite's first from an audio device, and the first from a device in setup
mode; `test_captured_replay.py`'s module docstring should say so.

**Unit tests.**

- `parse_access_points` against the real three-AP payload.
- `parse_current_access_point` returns `None` for the unconfigured sentinel.
- `is_open` matrix: `NONE` and `WEP/NONE` open; `WPA2/PSK`, `WEP`, `EAP`
  secured.
- Payload builders asserted byte-for-byte against the bodies the hardware run
  actually sent.
- `NET_WIFI_ALREADY_CONNECTED` does not raise; other `NET_*` codes raise
  `VizioWifiError` with the right `result`.
- `join_access_point` ordering: `current_access_point` PUT strictly precedes
  `set_wifi_password` PUT.
- Hidden path constructs the documented payload and issues exactly one PUT.
  Payload-shape tests only — there is no hardware behind this path and the tests
  must not imply otherwise.

**Session lifecycle.**

- Scan stops on clean exit, on an exception raised in the body, and when `join`
  raises.
- A failing `stop_ap_scan` does not mask the caller's exception.
- `join` is re-callable within one session.

**CLI**, following `test_cli_commands.py` conventions:

- Wizard with mocked prompts drives scan → select → join.
- Open network skips the password prompt entirely.
- Hidden entry (`0`) prompts for an SSID.
- `AUTH_REJECTED` and `MISSING_PASSWORD` both re-prompt for the password.
- Bare-IP host resolution probes both default ports.

## Documentation

`docs/protocol-notes.md` §32 already records the flow; it needs updating from
"APK DERIVED — NOT HARDWARE VERIFIED" to reflect the hardware confirmation, plus
the three facts the run established (port 9000, no auth, `NAME`-only) and the
hidden path remaining unverified. README gets a short provisioning example.
