# SmartCast Protocol Notes

> **Update — APK decompile findings folded in.** The official Vizio Android
> app (`com.vizio.vue.launcher` 5.0.0) was decompiled and analyzed; full
> findings in `android-app-findings.md`. Where APK evidence overrides
> earlier inferences, the relevant section below is annotated **APK
> CONFIRMED** or **APK CORRECTED**.

This document captures protocol quirks observed in the wild and **how
`vizaio` handles each one**. Every entry is classified by evidence
strength so future maintainers can tell which behaviors are device-imposed
(must port forward) vs which were workarounds in `pyvizio` that we've
deliberately not carried over.

Sources cross-referenced:

- `pyvizio` git history, code comments, and issue tracker
- `vizio-smart-cast` (JavaScript, original community RE work by exiva)
- `smartcastctl` (Go, independent reverse engineering)
- Home Assistant `vizio` integration source
- `pyvizio` open issues classified against this redesign

Classification key:

| Confidence | Meaning |
|------------|---------|
| **REAL** | Confirmed by ≥2 independent implementations OR explicit pyvizio commit message tying it to firmware behavior |
| **REAL-INFERRED** | One implementation handles it, defensive comments explain why, no contradictory evidence |
| **HARDWARE-VERIFY** | pyvizio handles it but origin is undocumented; behavior may be a hack or unnecessary defense — confirm against real hardware before locking into our tests |
| **DROP** | pyvizio carried it but evidence suggests it was unnecessary or unrelated to real device behavior |

---

## 1. Mixed-case CNAMEs in responses

**Confidence:** REAL

**Behavior:** Devices return ITEMS with `CNAME` keys in inconsistent case
across firmware versions. A single field may appear as `power_mode` on one
firmware and `POWER_MODE` on another. The casing of the *envelope key* itself
(`STATUS`, `ITEMS`, `RESULT`) is also inconsistent.

**Evidence:**

- pyvizio `_parse.py` module docstring: "All parsing uses case-insensitive
  key lookups since SmartCast responses have inconsistent casing."
- vizio-smart-cast (JS) README documents inputs as case-insensitive.
- pyvizio `_ci_get` predates v2 redesign and is used pervasively across all
  parsers — defensive but consistent.

**Our handling:** `Response.from_json` normalizes ALL keys to lowercase at
the wire boundary, once. Downstream parsers and the device class never see
mixed case for standard `STATUS`/`ITEMS` envelopes — pyvizio's pervasive
`_ci_get` lookups inside parsers are unnecessary here. A small `_ci_get`
helper does exist in `parse.py` for one specific case: the `/state_extended`
endpoint uses a non-standard flat-keyed payload that bypasses
`Response.from_json`, so `parse_state_extended` does its own
case-insensitive lookups. Every other parser relies on boundary
normalization.

**Tests:** Fixtures emit both casings (`POWER_MODE` and `power_mode`).
`Response.from_json` produces identical `Item` for both. See
`tests/test_wire.py::TestCnameNormalization`.

---

## 2. CNAME aliases (`POWER_MODE` ↔ `power_mode` etc.)

**Confidence:** HARDWARE-VERIFY (resolved into Real after analysis)

**Behavior:** pyvizio maintains an explicit alias dict mapping uppercase
cnames to lowercase. Cross-referenced agents disagreed:

- Agent B (related projects) said real device behavior with separate
  evidence.
- Agent A (pyvizio archaeology) noted the alias dict is duplicative with
  `_ci_get` and has no commit message explaining why both exist.

**Resolution:** Once `Response.from_json` lowercases all keys including
`cname` field values, the alias dict becomes redundant. Our `Item.cname`
is always lowercase. We do NOT carry the alias dict forward.

**Tests:** Fixtures with `cname: "POWER_MODE"` and `cname: "power_mode"`
both produce `Item.cname == "power_mode"`.

**Risk:** If hardware testing reveals devices return cnames where casing
differs from the request path's casing (i.e., the device thinks
`POWER_MODE` is a different setting than `power_mode`), we'll need to
revisit. Unlikely — pyvizio's case-insensitive matching has worked for
years across thousands of devices.

---

## 3. Multiple endpoint paths per logical endpoint (`_ALT_*` fallback)

**Confidence:** REAL

**Behavior:** ESN, serial number, and version live at different URL paths
on different firmware generations:

- Newer firmware: `/menu_native/dynamic/tv_settings/admin_and_privacy/system_information/tv_information/{esn,serial_number,version}`
- Older firmware: `/menu_native/dynamic/tv_settings/system/system_information/tv_information/{esn,serial_number,version}`

**Evidence:**

- pyvizio commit `d08e20e`: "consolidate device configuration into
  DeviceConfig dataclass" — both paths defined per device class.
- exiva `Vizio_SmartCast_API` issue #15: "New firmware update" references
  API path changes across firmware versions.
- pyvizio open issue #135 ("Some sources no longer work with Home Assistant"
  after firmware update) is exactly this category.

**Our handling:** `EndpointSpec.paths` is a tuple. The client tries each
path in order, falling through to the next on `VizioNotFoundError`. The
fallback is protocol-shaped, lives in the client, not in `_device.py`.

**Tests:** Fixtures simulate primary returning a malformed/empty response,
client falls back to alt path, returns the alt's data. See
`tests/test_client.py::TestEndpointFallback`.

---

## 4. NAME_SPACE 2 ↔ 4 equivalence in app matching

**Confidence:** REAL

**Behavior:** When matching a `current_app` response's `NAME_SPACE` field
against the bundled app catalog, namespaces 2 and 4 are treated as
equivalent. The same app may appear with `NAME_SPACE: 2` on one firmware
and `NAME_SPACE: 4` on another.

**Evidence:**

- pyvizio commit `219260d` (PR #97, 2020-04-11): "add newly discovered
  apps and make NAME_SPACE of 2 and 4 interchangeable"
- pyvizio bundled apps catalog has both forms for the same app
  (Prime Video, Plex, Pluto TV).
- vizio-smart-cast (JS) accepts `name_space` as a free integer parameter
  to `app.launch()`, suggesting client cannot assume one canonical value.

**Our handling:** `find_app_name(config, catalog)` treats `NAME_SPACE in {2, 4}`
as equivalent during matching. Documented as a constant
`EQUIVALENT_NAME_SPACES = (2, 4)` so future-Vizio-firmware additions are
greppable.

**Tests:** App catalog entry has `NAME_SPACE: 2`. Device returns
`NAME_SPACE: 4` for the same app. `find_app_name` returns the catalog name.
And vice versa. See `tests/test_apps.py::TestNamespaceEquivalence`.

---

## 5. NAME_SPACE 0 → "Cast" sentinel

**Confidence:** REAL

**Behavior:** When the device reports `NAME_SPACE: 0`, the active "app"
is the SmartCast home screen / Cast UI rather than a third-party app.

**Evidence:**

- pyvizio code comment: "So far only the SmartCast home screen appears
  to use the NAME_SPACE of 0"
- exiva docs reference NAME_SPACE 0 for the home screen.
- Treated as observation, not a fix.

**Our handling:** `find_app_name` checks `config.name_space == 0` early
and returns the `APP_CAST` sentinel string. Independent of the bundled
catalog (no catalog entry for "Cast" — it's a synthetic app).

**Tests:** `find_app_name(AppConfig("any_id", 0), [])` returns `"Cast"`.

---

## 6. Synthetic `current_input` item in inputs response

**Confidence:** APK CORRECTED — pyvizio's filter is unique to pyvizio

**Behavior:** When fetching the inputs list, the device returns
inputs each with `enabled` and `visible` flags. pyvizio filters by
*name* (cname == "current_input"). The official app filters by
*flags* (enabled OR visible) and rotates CAST to the top of the list.
The "current input" is fetched via a separate `devices/current_input`
endpoint, not by name-matching in the inputs list.

**Evidence:**

- APK `SCPLCommandsManager.sortedAvailableInputs()` filters by
  `getEnabled() || getVisible()`, not by cname.
- Vizio-smart-cast (JS) does not document a name-based filter.
- pyvizio's name filter has no linked issue or PR explaining it.

**Our handling:** `parse_inputs(response)` filters by `enabled OR
visible` to match the official app's behavior. The current input is
determined via the separate `devices/current_input` endpoint, not by
name-matching in the inputs list. The `InputInfo.is_current` flag is
populated by cross-referencing those two responses in `Vizio.get_inputs()`.

**Risk:** Low — matches the proven behavior of the official app.

**Tests:** Fixtures include the synthetic `current_input` item. Tests
assert it is filtered from `parse_inputs` output AND drives the
`is_current` flag on the named input.

---

## 7. Setting types filter (excluding `cast`, `input`, `devices`, `network`)

**Confidence:** APK CONFIRMED — DROP, pyvizio invention

**Behavior:** pyvizio filters four cnames from setting types. The
official app does not — it surfaces every menu segment under
`menu_native/dynamic/<root>` and lets the user navigate naturally.

**Evidence:**

- APK has no equivalent filter at the application layer.
- Settings are surfaced via menu-tree endpoints in their device-native
  shape; the app trusts the response.

**Our handling:** Initial implementation: do NOT filter. Return all
setting types as the device reports them. If hardware testing shows that
e.g. `get_settings("network")` returns malformed data or hangs, we'll
add a documented filter at that point.

**Risk:** Returning a setting type that doesn't actually contain settings
might confuse callers. Mitigation: `get_settings(name)` raises
`VizioResponseError` if the response shape doesn't match what
`parse_settings` expects, surfacing the issue rather than silently
returning garbage.

**Tests:** Hardware smoke test (#29) attempts `get_setting_types()` on a
real TV and confirms what's actually returned.

---

## 8. Volume read via `audio.volume` setting (no dedicated endpoint)

**Confidence:** REAL

**Behavior:** Unlike `get_power_state` which has a dedicated endpoint,
volume is read by GET on the `audio.volume` setting. The device exposes
no `/state/device/volume` equivalent.

**Evidence:**

- pyvizio has no alternative volume endpoint.
- vizio-smart-cast (JS) does the same.
- Confirmed in pyvizio open issue #125 ("Vizio Elevate sound bar volume
  control issue") which discusses volume as a setting.

**Our handling:** `get_volume()` is a thin wrapper over
`get_setting("audio", "volume")`. Returns `int`. Raises `VizioError` on
device failure.

---

## 9. "No app running" indication

**Confidence:** HARDWARE-VERIFY (low risk — both cases handled)

**Behavior:** When no app is running (TV is on an HDMI input or off),
the `current_app` response has either:

- `VALUE: null` explicitly, OR
- `VALUE` key is absent entirely.

pyvizio handles both via falsy check (`if not value: ...`). It's unclear
which form the device actually uses — pyvizio's defensive code is neutral.

**Evidence:** No tests, fixtures, or code comments distinguish.

**Our handling:** Same — `parse_current_app_config` returns `None` when
either `VALUE` is missing or `VALUE` is None. The `Vizio.get_current_app`
method maps `None` to a `NO_APP_RUNNING` sentinel string.

**Risk:** None — both cases handled identically. We document for clarity.

**Tests:** Fixtures cover both shapes. Same expected output.

---

## 10. Volume scale per device class (TV=100, Speaker=31, Crave=24)

**Confidence:** REAL

**Behavior:** Device families use different volume scales:

- TVs: 0–100
- Soundbars: 0–31
- Crave 360: 0–24

This is hardware specification, not firmware quirk.

**Evidence:**

- pyvizio `DeviceConfig.max_volume` per class.
- pyvizio open issue #125 documents user confusion: setting volume to 1
  on a soundbar maxes it out at ~3% perceived, because soundbar volume
  scale is 0-31 not 0-100.

**Our handling:** Lives on `DeviceProfile.max_volume`. `volume_up(steps=N)`
just sends N key presses; the *actual* volume is the device's
responsibility. `get_volume()` returns the raw value the device reports —
callers can divide by `device.profile.max_volume` if they want a 0-1 range.

**Tests:** `tests/test_profiles.py` asserts each profile's `max_volume`.

---

## 11. Device-type auto-detection via auth check

**Confidence:** DROP (in current form)

**Behavior:** pyvizio's `async_guess_device_type` distinguishes TV from
soundbar by attempting an unauthenticated GET on the audio settings
endpoint. Soundbar = succeeds (no auth required). TV = fails (auth
required).

**Evidence:**

- Only pyvizio implements this. vizio-smart-cast (JS) and smartcastctl
  require explicit device class.
- Logic correct in principle (soundbars don't require auth, TVs do)
  but fragile: depends on a 401-vs-200 distinction that may change.

**Our handling:** No auto-detection. Constructor requires explicit
`device_type` (or `profile`). The CLI `pair` wizard can suggest a device
type based on discovery results (zeroconf advertises `_viziocast._tcp`
for TVs and DIAL for soundbars), but the library does not guess.

**Rationale:** Auto-guessing was a feature that masked configuration
errors. Better to fail loudly with `VizioInvalidParameterError` if the
user specifies the wrong type than to silently guess wrong.

---

## 12. Pairing leaves devices stuck (open issue #175 inferred)

**Confidence:** REAL (behavior), our fix is preventive

**Behavior:** Calling `start_pair` without a matching `pair` or
`stop_pair` leaves the device in pairing mode until reboot. Many pyvizio
callers forget to call `stop_pair` on error paths. Similar symptoms in
issue #175 (TV stops responding to commands after some sequence).

**Our handling:** Pairing is exposed only as an async context manager:

```python
async with vizio.pair_session(device_id, name) as session:
    # device in pairing mode
    auth_token = await session.complete(pin)
# context manager exits — calls cancel_pair if complete() didn't fire
```

The `__aexit__` checks an internal `_completed` flag. If the session
exits with the flag false (exception, cancellation, complete() never
called), it calls `cancel_pair` automatically. If `_completed` is true
(a successful pairing), the device is paired — no cancel sent.

**Tests:** `tests/test_device.py::TestPairSession` covers four paths:
(1) successful complete — no cancel sent, (2) exception inside `with`
block — cancel sent, (3) `complete()` raises — cancel sent, (4)
KeyboardInterrupt during `complete()` — cancel sent.

---

## 13. Hashval race in setting writes

**Confidence:** REAL (open issues #135, #140 are this exact failure)

**Behavior:** Writing a setting requires a `HASHVAL` from a prior GET.
The device may regenerate the hashval between GET and PUT, causing the
PUT to fail with `STATUS.RESULT: invalid_parameter`. pyvizio surfaces
this as `VizioInvalidParameterError`; user sees "FAILURE" on the second
attempt and the operation appears flaky.

**Our handling:** `set_setting()`:

1. If caller passed `hashval=N`, send the PUT with that hashval directly.
2. Else, GET the current hashval, send PUT with it.
3. If PUT raises `VizioInvalidParameterError` AND we did the GET in step
   2, retry the GET+PUT *once*. If the second PUT also raises, propagate.

The retry is opt-out via `set_setting(..., retry=False)`. PUTs with a
caller-supplied hashval do NOT auto-retry — caller has full control.

**Tests:** Fixture for first PUT returns invalid_parameter, second
GET returns fresh hashval, second PUT succeeds. Test asserts caller sees
no exception.

---

## 14. Connection failures with valid credentials (open issues #133, #151)

**Confidence:** REAL (multiple users report)

**Behavior:** pyvizio fails to connect even when curl succeeds against
the same endpoint with the same credentials. Suspected causes:

- Per-request `ClientSession` creation (closed connections, cert cache
  misses)
- Inconsistent SSL handling across aiohttp versions
- Port resolution failing silently and trying the wrong port

**Our handling:**

- `Vizio` owns its `ClientSession` for the instance lifetime (created in
  `__aenter__`, closed in `__aexit__`/`aclose`).
- SSL is disabled in exactly one place — a module-level
  `_SSL_CONNECTOR = aiohttp.TCPConnector(ssl=False)`.
- Port resolution: probe `DEFAULT_PORTS` once, cache the winning port for
  the session. On `VizioConnectionError`, raise immediately — do not
  silently re-probe.

**Tests:** Hardware smoke test (#29) — replicate user's curl with our
client against same device, must succeed.

---

## 15. CLI argument escaping (open issue #170)

**Confidence:** REAL — fixed by Typer

**Behavior:** pyvizio CLI uses raw shell argument parsing; values
containing shell metacharacters (`*`, `?`, etc.) need user-side
quoting that's not always obvious.

**Our handling:** Typer with `Annotated[str, typer.Argument(...)]`
correctly escapes/quotes arguments. CLI tests use `CliRunner` which
exercises the same parsing path.

**Tests:** `tests/test_cli_*.py` includes profile names like `"Game*"`,
`"Live TV"` (space), `"Wi-Fi"`. All accepted as single arguments.

---

## 16. Power command inversion (open issue #163)

**Confidence:** REAL bug in pyvizio (now fixed in main branch but worth
documenting the test)

**Behavior:** A pyvizio user reported `power on` turning the TV off.
Looking at the codebase, this seems to have been a transient regression
(commit history shows correctness now), but worth a regression test.

**Our handling:** `power_on()` sends a `POW_ON` key press. `power_off()`
sends `POW_OFF`. The respective key codes `(11, 1)` and `(11, 0)` come
from the per-profile keymap.

**Tests:** `power_on` PUTs `KEYLIST` containing `[{"CODESET": 11, "CODE": 1, ...}]`.
`power_off` PUTs `KEYLIST` containing `[{"CODESET": 11, "CODE": 0, ...}]`.
Asserted at the payload level — no possibility of a sign inversion.

---

## Summary table

| # | Quirk | Confidence | Handling | Test fixture? |
|---|-------|-----------|----------|---------------|
| 1 | Mixed-case CNAMEs | REAL | Normalize once at wire boundary | yes |
| 2 | CNAME aliases dict | REAL→DROP | Subsumed by #1 | yes |
| 3 | `_ALT_*` endpoint fallbacks | REAL | `EndpointSpec.paths` tuple | yes |
| 4 | NAME_SPACE 2↔4 | REAL | `EQUIVALENT_NAME_SPACES` constant | yes |
| 5 | NAME_SPACE 0 → Cast | REAL | Sentinel in `find_app_name` | yes |
| 6 | Synthetic current_input | HARDWARE-VERIFY | Filter + `is_current` flag | yes |
| 7 | Setting types filter | HARDWARE-VERIFY | NOT carrying — see how device behaves | hardware test |
| 8 | Volume via setting | REAL | `get_volume` wraps setting | yes |
| 9 | "No app running" shape | HARDWARE-VERIFY | Both shapes handled | yes (both) |
| 10 | Volume scale per class | REAL | `DeviceProfile.max_volume` | yes |
| 11 | Auto-detect device type | DROP | Explicit constructor arg | n/a |
| 12 | Stuck pairing | REAL | `pair_session` context manager | yes (4 paths) |
| 13 | Hashval race | REAL | Auto-retry once on `invalid_parameter` | yes |
| 14 | Connection flakiness | REAL | Owned session + cached port | hardware test |
| 15 | CLI argument escaping | REAL | Typer | yes |
| 16 | Power inversion | REAL bug | Payload-level assertion | yes |
| 17 | HASHVAL is opaque | REAL | GET-then-PUT, retry on race | yes |
| 18 | Auth tokens durable | LIKELY | No refresh logic | n/a |
| 19 | KEYLIST sequential, max unknown | PARTIAL | Chunk if N > 20 | yes |
| 20 | Concurrent requests | LIKELY OK | Semaphore default 1 | n/a |
| 21 | Pairing cooldowns | UNKNOWN | Context manager handles | hardware test |
| 22 | `/state_extended` bulk state | APK CONFIRMED | Capability-gated aggregate | yes |
| 23 | Direct battery state endpoints | APK CONFIRMED | `/state/device/{battery,charging}` | yes |
| 24 | Crave Go/360/Pro variants | APK CONFIRMED | Three preset profiles | yes |
| 25 | Tight default timeouts | APK CONFIRMED | read=10/write=3/connect=2 | n/a |
| 26 | `AUTH` (not `Authorization`) | APK CONFIRMED | Literal `AUTH` header | yes |
| 27 | Zeroconf-only discovery | APK CONFIRMED | SSDP optional fallback | yes |

---

## 17. HASHVAL is opaque and server-assigned

**Confidence:** REAL — confirmed OPAQUE with HIGH certainty

**Behavior:** SmartCast settings require a `HASHVAL` integer for every PUT.
The value is server-assigned, not client-computable. It changes between
GETs of the same setting. No reverse-engineering effort across years of
community work has cracked the algorithm.

**Evidence:**

- pyvizio v2 design doc line 269: "We cannot compute hashvals client-side
  — the algorithm is unknown."
- vizio-smart-cast (JS) does GET-then-PUT — no client computation.
- exiva official API docs: "Obtain HASHVAL values from the SETTINGS_CNAME
  ITEMS array" — treats it as opaque.
- No commits, issues, or threads suggest anyone has cracked it.

**Implications for vizaio:**

- 2 round trips on every setting write is the unavoidable baseline.
- `set_setting(..., hashval=N)` skips the GET when caller has it cached —
  this is the optimization, not the algorithm.
- On `VizioInvalidParameterError` from a PUT, retry once: do fresh GET
  then PUT. Most "stale hashval" races resolve on the second attempt.
- Caller-side caching (HA pattern: poll once, write multiple times)
  meaningfully reduces device load.

**Important context:** HASHVAL agent argued issues #135/#140/#175 are
likely caused by **device saturation under rapid GET-PUT bursts**, not
missing optimization. Implication: the semaphore + bounded retry are
necessary, not paranoid.

**Tests:** `test_device.py::TestSetSettingRace` covers: (1) caller passes
hashval, no GET fired, (2) no hashval supplied, GET-then-PUT, (3) PUT
returns invalid_parameter, second GET+PUT succeeds.

---

## 18. Auth tokens are durable bearer tokens

**Confidence:** LIKELY — no TTL, no MAC, no revocation short of re-pair

**Behavior:**

- **No expiration.** Tokens issued by `pair` flow have no documented TTL.
  pyvizio has zero refresh logic. HA's vizio integration treats them as
  permanent.
- **Bound to (device_id, device_name) at pairing time, not to client IP
  or MAC.** Token is portable across networks.
- **Bearer semantics.** `AUTH: <token>` header, plaintext, no signature.
- **Revocation only via factory reset or re-pairing the same device_id**
  (which overwrites the old token).
- **Device tracks no per-token sessions.** Multiple clients using the same
  token simultaneously appears to work fine; pyvizio's semaphore is
  defensive, not session-limit-driven.

**Evidence:**

- pyvizio `_client.py`: `headers["AUTH"] = self.auth_token` — bare bearer.
- No refresh, expiry, or rotation code in pyvizio or HA.
- Commit `3c543f5` rationale: "device overloading", not session limits.

**Implications for vizaio:**

- Treat tokens as durable. No refresh logic needed.
- Document `device_id` arg to `pair_session` as the **identity used at
  pairing time** — calling `pair_session(device_id="ha-coord", ...)` later
  with the same device_id will overwrite the prior token.
- Issue #175 ("TV stops responding") is unlikely to be auth-related; more
  likely device saturation from GET-PUT burst (see #17).

**Tests:** Token round-trip in pairing fixtures. No TTL test (no TTL to
test).

---

## 19. KEYLIST: sequential, no client-side cap

**Confidence:** APK CONFIRMED

**Behavior:**

- `{"KEYLIST": [k1, k2, k3]}` is processed sequentially by device firmware.
- Only `"ACTION": "KEYPRESS"` is supported. The official app's full key
  catalog (156 entries) hardcodes `"KEYPRESS"` everywhere; no
  `KEYDOWN`/`KEYUP`/`KEYHOLD` exists.
- The official app sends arbitrary-length KEYLISTs in a single PUT — no
  client-side chunking. Multi-character keyboard search submits long
  lists without splitting.

**Evidence:**

- APK `KeyCommandRequest.keyListItem` is a single Kotlin `List<KeyListItem>`
  with no pre-send transform.
- Searches for `chunked|MAX_KEY|maxKey|keyList\.size` over `com/vizio`
  match only `VoiceSearch` (audio bytes) and `HexEncodeDecodeUtils`
  (string hex pairs) — never the KEYLIST.
- All 156 `KeyCommandItem` constructors pass `"KEYPRESS"` literally.

**Implications for vizaio:**

- `volume_up(steps=N)` sends a single PUT for any N up to a defensive
  cap of **50**. Above that, chunk into multiple PUTs through the
  instance semaphore.
- Defensive cap exists only to handle worst-case device buffers — not
  required by protocol.
- No held-key support — protocol limitation, not a bug.

**Tests:** `test_payloads.py::TestKeyPress` covers N=1, N=5, N=20.
Hardware test #29 attempts N=50 to probe the actual limit.

---

## 20. Concurrent requests: device handles, but be defensive

**Confidence:** LIKELY OK — semaphore is preemptive

**Behavior:** No documented crashes from concurrent GETs/PUTs. pyvizio's
`max_concurrent=1` semaphore was added preemptively for "device
overloading," not in response to a specific failure mode. The device
firmware appears to serialize internally.

**Evidence:**

- pyvizio commit `3c543f5` adding semaphore cites "device overloading,"
  not crash reports.
- No issues describing 2-concurrent-GETs failures.
- HA core uses pyvizio with default semaphore; no concurrency-specific
  bug reports.

**Implications for vizaio:**

- Keep instance-level `asyncio.Semaphore(1)` as default — matches pyvizio.
- Allow caller override via constructor `max_concurrent_requests=N`,
  documented as advanced/experimental.
- DO NOT advertise concurrent requests as a feature — observed safety is
  not a guarantee.

**Tests:** No unit tests can validate this — it's a device behavior. Note
in protocol notes only.

---

## 21. Pairing token cooldowns / reuse

**Confidence:** LIKELY safe to retry, but unconfirmed

**Behavior:**

- `cancel_pair` followed immediately by `begin_pair` with the same
  `device_id` — no documented cooldown, but no positive confirmation
  either.
- `begin_pair` with a `device_id` that already has a successful pairing
  on the device — likely overwrites the old token, but unconfirmed.
- Partial pairings (started, never completed, never canceled) — likely
  cleared on reboot, but unconfirmed.

**Evidence:** No code, comments, or issues describe pairing cooldowns.
pyvizio's stateless approach (no client-side pairing tracking) implies the
device handles its own state.

**Implications for vizaio:**

- `pair_session.__aexit__` cancels on error — safe in the common case.
- If user retries pairing after a failure, our context manager handles it
  cleanly. If hardware testing reveals cooldowns, add backoff to the
  retry path.
- Document for HA integrators: re-pairing the same `device_id` will
  invalidate the previous token. Don't pair "ha-coord" twice expecting two
  parallel auth tokens.

**Tests:** `test_device.py::TestPairSession` covers our context-manager
behavior. Hardware test #29 explicitly tries cancel+retry to probe for
cooldown.

---

---

## 22. `/state_extended` — bulk state polling

**Confidence:** APK CONFIRMED

**Behavior:** Modern firmware exposes `/state_extended` (auth-required)
that returns multiple state values in one round trip. Capability is
advertised by the device in `state/device/deviceinfo` under
`SCPL_CAPABILITIES.state_extended`.

**Evidence:**

- APK `V2SCPApi.extendedState(authToken)` — calls `GET /state_extended`.
- APK `ScplCapabilities.state_extended` boolean field with `@SerialName`
  annotation.

**Our handling:** Add `Endpoint.STATE_EXTENDED` to the resolver. Add a
high-level `Vizio.get_state_extended()` method. `Vizio.get_device_info()`
opportunistically uses `/state_extended` when supported, falling back to
N individual GETs when not — no breaking change for older firmware.

**HA implication:** A polling coordinator can fetch volume, mute, input,
power, and current app in one round trip instead of five. Reduces
device load (relevant given saturation concerns from issue #175) and
HA scan-interval pressure.

---

## 23. Direct `/state/device/*` endpoints for battery (Crave)

**Confidence:** APK CONFIRMED

**Behavior:** Crave devices expose `/state/device/battery_level` and
`/state/device/charging_status` as direct state endpoints, not as
menu-tree settings. pyvizio's menu-tree paths still work but the direct
paths are the canonical ones used by the official app.

**Evidence:**

- APK `V2SCPApi` defines GETs at `/state/device/battery_level` and
  `/state/device/charging_status`.
- These are advertised in `SCPL_CAPABILITIES`.

**Our handling:** Use the direct `/state/device/*` paths. Documented in
`_endpoints.py`.

---

## 24. Crave variants (Go / 360 / Pro)

**Confidence:** APK CONFIRMED

**Behavior:** The official app distinguishes three Crave models by
hardware string:

- **Crave Go** = `SP30-E0`
- **Crave 360** = `SP50-D5`
- **Crave Pro** = `SP70-D5`

All share the audio-settings tree, all have batteries.

**Our handling:** Three separate `DeviceProfile` presets
(`CRAVE_GO_PROFILE`, `CRAVE360_PROFILE`, `CRAVE_PRO_PROFILE`) and
matching `DeviceType` enum members. Capability flags and keymap
identical until hardware testing reveals differences.

---

## 25. Default timeouts (read=10s, write=3s, connect=2s)

**Confidence:** APK CONFIRMED

**Behavior:** The official app uses tight per-request timeouts:

- TCP connect: 2 seconds
- Write request body: 3 seconds
- Read response body: 10 seconds

**Evidence:** APK `EmbeddedConnectionConfig.java` defaults.

**Our handling:** Match these exactly. The `Vizio` constructor's
`timeout=` arg accepts a single float (sets the read timeout) or an
`aiohttp.ClientTimeout` for finer control.

---

## 26. `AUTH` header (not `Authorization`)

**Confidence:** APK CONFIRMED

**Behavior:** Authenticated requests use a literal `AUTH: <token>` header
— not `Authorization`, not `Bearer`. pyvizio gets this right; documenting
to lock it in.

**Evidence:** APK `SmartCastHeaders.HEADER_KEY_AUTH = "AUTH"`. There's
vestigial `Authorization`-header construction in
`EmbeddedConnectionConfig.java:34` but the side-effect is discarded
(dead code from an earlier design).

**Our handling:** `SmartCastClient` adds `AUTH: <token>` for auth-required
endpoints. Also sends `VIZIO-SmartCast-Source: vizaio` to
identify the client honestly (matches the app's pattern of sending
`SMARTCAST_ANDROID`).

---

## 27. Discovery — zeroconf only, no SSDP

**Confidence:** APK CONFIRMED

**Behavior:** The official app uses Android NSD with service type
`_viziocast._tcp` exclusively. No SSDP, no DIAL.

**Evidence:** APK `VizioServiceDiscoveryClientKt.SERVICE_TYPE =
"_viziocast._tcp"`. No SSDP code path anywhere in `com.vizio`.

**Our handling:** `discover()` runs zeroconf primarily. SSDP is kept as
an *optional* fallback because pyvizio's SSDP scan does catch some
older soundbars that respond to DIAL but don't advertise mDNS — pyvizio
issue history confirms this. The fallback is opt-in via
`discover(include_ssdp=True)` and not the default.

---

## 28. WebSocket SCPL — event subscription

**Confidence:** APK CONFIRMED + HARDWARE VERIFIED

**Behavior:** The TV exposes a WebSocket interface alongside REST.
Subscription is opted into by **HTTP** `PUT /event/register` (not a WS
frame), then a `wss://<host>:<ws_port>/` connection is opened and the
device pushes `{"URI": "<cname>", ...}` JSON frames when registered
properties change. There is no per-cname subscription envelope on the
socket — registration is a single global toggle.

**Our handling:** `Vizio.subscribe_events()` returns an async-iterable
`EventStream` (`src/vizaio/_websocket.py`). The implementation:

- Sends `PUT /event/register` with body `{"REQUEST":"MODIFY","VALUE":"TRUE"}`
  before opening the WS. (Hardware probe: omitting `VALUE` returns
  `INVALID_PARAMETER`; `VALUE: true` (JSON bool) crashes the device's
  parser and returns HTTP 500 with HTML body. The string `"TRUE"` is
  the only form that works on at least one firmware revision —
  3.720.9.1-1 / VHD24M-0810.)
- Decodes incoming frames into `StateEvent` typed dataclasses with the
  raw envelope preserved on `.raw` for unknown URIs.
- Auto-reconnects on disconnect by default; `auto_reconnect=False`
  ends the iterator on first drop.
- Surfaces "device rejected event-register" as
  `VizioUnsupportedError("device rejected event-register …")` so
  callers can fall back to polling on firmware that doesn't support
  the surface.

See `docs/websocket-protocol-notes.md` for the full protocol writeup
(URL/port discovery, header set, payload shape, reconnect behavior,
known-URI taxonomy).

**Why it matters:**

- Sub-100ms state updates vs. ~10s polling lag.
- Eliminates the GET/PUT burst pattern implicated in pyvizio issue #175
  ("TV stops responding").
- Lower device load (one long-lived connection vs. dozens of round trips
  per minute).

**Evidence:**

- APK `V2SCPWebsocketApi` referenced at `DeviceCommandBuilder.java:360`.
- `eventRegister` operation, body shape captured live.
- Voice search uses a separate WebSocket (`VoiceSearch.java`) — out of
  scope for our library.

---

## Hardware verification list

The following items can only be confirmed against real hardware. Capture
in #29:

- **Quirk 6:** Does `get_inputs()` actually return a synthetic
  `current_input` item, or is the filter unnecessary?
- **Quirk 7:** What does `get_setting_types()` return on a real TV? Are
  `cast`, `input`, `devices`, `network` legitimate setting categories or
  navigation menus?
- **Quirk 9:** When TV is on HDMI (not running an app), what's the exact
  shape of the `current_app` response? `VALUE: null` or `VALUE` absent?
- **Quirk 14:** Does the new owned-session client succeed where pyvizio
  fails on user-reported flakiness?
- **Issue #160:** Soundbar WiFi configuration — what endpoint does the
  official app use?
- **Quirk 19:** What is the actual max KEYLIST length? Try N=20, 50, 100.
- **Quirk 21:** Pairing cooldown after cancel? Cancel and immediately
  retry from the same device_id; observe.
- **Issue #175 reproducer:** Run a tight GET+PUT loop on a setting for ~30
  seconds. Does the device become unresponsive? If yes, that's the
  saturation hypothesis confirmed and we know to recommend lower poll
  rates.

For each, capture the device response into `tests/_fixtures.py` as a
real captured fixture (with a `# captured from real Vizio M65Q7-H1
firmware 4.x.y.z` comment) and lock the behavior into a test.

---

## Real-device verification — VHD24M-0810, firmware 3.720.9.1-1

Captured payloads live in `tests/captured/` (PII-scrubbed) and are
replayed end-to-end through the parser layer by
`tests/test_captured_replay.py`. The findings below either confirm,
correct, or extend the APK-derived sections above.

### V1 — Confirmed

- **Quirk 1 (mixed-case CNAMEs):** verified — fixture cnames
  arrive uppercase; parser normalizes once at the wire boundary.
- **Quirk 6 (synthetic current_input):** modern firmware does **not**
  include a synthetic `current_input` item inside the inputs response.
  Library now fetches `current_input` separately and passes the value
  to `parse_inputs(current_input_name=...)`.
- **Quirk 7 (setting types filter):** confirmed pyvizio's filter is
  unnecessary. Captured `setting_types` returned all 9 categories
  (`picture, audio, network, channels, accessibility, devices, system,
  admin_and_privacy, cast`) — including the four pyvizio filtered out.
- **Quirk 9 (no-app shape):** captured payload from the SmartCast Home
  screen has `app_id='1', name_space=4` with a JSON-state `MESSAGE`,
  not `VALUE: null`. The "no app running" fall-through still applies
  for HDMI inputs (current_app reports a different `app_id` not in
  the catalog → `_UNKNOWN_APP` sentinel).
- **Quirk 14 (connection flakiness):** owned-session pattern eliminated
  the pyvizio symptoms — no spurious "could not connect" failures
  observed during a multi-hour session against this device.
- **Quirk 18 (auth token durability):** fully verified.
  - Tokens are durable bearer credentials. No TTL.
  - **Re-pairing with the same `device_id` invalidates the previous
    token immediately.** Subsequent calls with the old token return
    raw HTTP 403 (not the SCPL envelope with `PAIRING_DENIED`).
    Library now maps HTTP 401/403 to `VizioAuthError`.
- **Quirk 21 (pairing cooldowns):** no cooldown observed. Re-pair
  with the same `device_id` succeeds immediately, generates a new
  token, and invalidates the old one.

### V2 — Corrected by hardware

- **Quirk 3 (multi-path endpoint fallback) — bigger than documented.**
  Modern firmware (3.720+) exposes the **aggregate**
  `/menu_native/dynamic/tv_settings/admin_and_privacy/system_information/tv_information`
  endpoint that returns all identity fields (tv_name, serial_number,
  model_name, firmware, cast_version, vizios, conjure, sc_config,
  input, audio_in, audio_out, hdr, vrr, frame_rate, resolution,
  bluetooth_version) in a single response. The per-field child paths
  (`.../tv_information/esn`, `.../tv_information/serial_number`, etc.)
  return `URI_NOT_FOUND` on this firmware — they exist only on older
  firmware. Library now prefers the aggregate, falling back to
  per-field on `URI_NOT_FOUND`.
- **Quirk 6 (input identifier disambiguation) — three-way zoo.**
  The PUT body for `current_input` carries the lowercase **cname**
  (e.g. `"hdmi2"`), **not** the display name (`"HDMI-2"`) or the
  meta_name (`"Mac"` after a user rename). Sending the wrong form:
  - display name → `RESULT: FAILURE`
  - meta_name → `RESULT: HASHVAL_ERROR`
  - cname (lowercase) → `RESULT: SUCCESS`

  Additionally, the `current_input.VALUE` returned by the device is
  **inconsistent** across input types:
  - `cast` input → returns `"SMARTCAST"` (the meta_name).
  - `hdmi2` input → returns `"HDMI-2"` (the display name) even when
    the user has renamed the input to e.g. `"Mac"`.

  Library exposes `InputInfo.cname` and the `set_input` resolver
  accepts cname / name / meta_name interchangeably.
- **Quirk 19 (KEYLIST length):** N=15 verified working in a single
  PUT. The 50-key chunk threshold is preserved as defensive
  insurance — actual device-side limit is presumably much higher.
  Numeric keys (codeset 0, codes 0-9) accepted by tuner-equipped
  models; the test device has no tuner and rejects them with
  `FAILURE` per-key, which is the correct user-visible behavior.

### V3 — New status codes discovered

The following `STATUS.RESULT` values were emitted by the live device
and are now first-class enum members in `vizaio.types.ResponseStatus`:

| Status | Mapping | Notes |
| --- | --- | --- |
| `URI_NOT_FOUND` | `VizioNotFoundError` | Modern firmware's "endpoint not exposed." HTTP 200 + this status, not HTTP 404. Triggers multi-path fallback. |
| `HASHVAL_ERROR` | `VizioInvalidParameterError` | More specific form of "your write parameters don't match current state." Triggers the existing hashval-race retry. |

Plus a transport-level mapping correction:

| HTTP status | Library exception | Notes |
| --- | --- | --- |
| 401, 403 | `VizioAuthError` | Auth rejection comes back as raw HTTP 401/403, not as an envelope. Re-pair invalidation surfaces here. |
| 500 | `VizioConnectionError` | Internal error from lighttpd (the static HTTP server hosting the SCPL REST API). |

### V4 — WebSocket SCPL update

Captured live: `PUT /event/register` with the body the APK research
inferred (`{"REQUEST": "MODIFY"}`) returns `INVALID_PARAMETER` on
firmware 3.720.9.1-1. The body that returns `SUCCESS` is
`{"REQUEST": "MODIFY", "VALUE": "TRUE"}` — the APK's `Body` model
serialized `VALUE` as null but newer firmware requires it as the
string `"TRUE"`. (`true` as a JSON bool crashes the device parser
and returns an HTML 500 page.)

Library's `EVENT_REGISTER_BODY` constant updated.

**WS server gating per-device:** even with `/event/register`
returning `SUCCESS`, this device does **not** actually run a
WebSocket server on the mDNS-advertised `wp` (8005) / `wsp` (8006)
ports (TCP refused). The SCPL REST API runs on lighttpd (verified
by `Server: lighttpd/1.4.67` header) — a static HTTP server with no
WebSocket support. The SCPL WS server is a separate binary that
isn't running on this firmware. Library probes by attempting the
register; on rejection re-raises as `VizioUnsupportedError` with a
helpful message.

The polling alternative for these devices is the `/state_extended`
endpoint (advertised under `deviceinfo.scpl_capabilities.state_extended`),
which returns power / current input / current app / screen mode /
media state in a single round trip with a flat-keyed envelope. See
`Vizio.get_state_extended()` and `StateExtended` for the typed
wrapper.

### V5 — Discovery: TXT record keys

Captured `_viziocast._tcp` TXT record uses **abbreviated keys** — the
APK research mentioned `wsPort` / `wssPort` but the actual TXT keys
on this firmware are `wp` (insecure WS port) and `wsp` (secure WS
port). Library updated to read these. Note: the ports are advertised
even when the WS server isn't running, so port presence is necessary
but not sufficient for WS support.

### V6 — Mute keymap is firmware-class-specific

Codes `(5,2)`, `(5,3)`, `(5,4)` all behave as `MUTE_TOGGLE` on this
firmware. `(5,5)` and `(5,6)` return `FAILURE` — they're not
recognized at all. Discrete `MUTE_ON` / `MUTE_OFF` codes appear
to be soundbar/Crave-specific, not TV.

`Vizio.mute()` and `Vizio.unmute()` are now state-aware: read mute
state, send `MUTE_TOGGLE` only on mismatch. Idempotent and works
across firmware variants. Power users wanting raw codes can call
`send_key("MUTE_ON")` directly — the keymap entry remains for
firmware that does honor it.

### V7 — INPUT_NEXT no-op observed

`INPUT_NEXT = (7, 1)` returns `SUCCESS` on this firmware but does not
actually cycle the input — verified twice (with all inputs idle, and
with HDMI-2 carrying a laptop signal). Codeset 7 only has code 1
valid; codes 0, 2-15 all return `FAILURE`. Codesets 14-16 codes 0-7
also return `FAILURE` for any candidate.

The keymap entry is preserved for firmware that does honor it; this
device's tuner-less budget panel may simply not implement input
cycling at the SCPL level.
