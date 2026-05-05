# Changelog

All notable changes to `vizio-smartcast` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/) once
1.0.0 ships. Until then, breaking changes may land in any
0.x.y release; the alpha-phase notes below describe behavior shifts
that integrators should be aware of.

## [Unreleased]

### Added

- `InputInfo.cname` — the device's canonical lowercase identifier
  (e.g. `"hdmi2"`, `"cast"`). Hardware probing revealed this is the
  **only** form `current_input` accepts in a PUT body — display name
  returns `FAILURE`, meta_name returns `HASHVAL_ERROR`. The field is
  populated automatically from the inputs response.
- `StateExtended` typed dataclass and `Vizio.get_state_extended()` —
  bulk single-round-trip poll returning power / current app / current
  input / screen mode / media state. Replaces five individual GETs for
  HA-style polling integrations on firmware that advertises
  `scpl_capabilities.state_extended`. Uses a non-standard envelope
  (flat top-level keys, no `STATUS`/`ITEMS`); parser handles the shape
  difference transparently.
- `DiscoveredDevice.ws_port` and `DiscoveredDevice.wss_port` —
  WebSocket port hints from the device's mDNS TXT record (`wp` and
  `wsp` keys). May be advertised but not actually open on every
  firmware revision (verified live on at least one model).
- `ResponseStatus.URI_NOT_FOUND` — modern firmware (~3.7+) returns
  this for paths it doesn't expose. Mapped to `VizioNotFoundError`
  so the multi-path endpoint fallback in `SmartCastClient` chains to
  the next candidate path.
- `ResponseStatus.HASHVAL_ERROR` — a more specific form of
  "your write parameters don't match current state." Mapped to
  `VizioInvalidParameterError` so the existing hashval-race retry
  path in `Vizio.set_setting` fires for both error codes.
- `Endpoint.TV_INFORMATION` — aggregate identity endpoint that
  returns all identity fields in one envelope. Used as a one-round-
  trip identity fetcher; the per-field endpoints stay as fallbacks
  for older firmware.
- `Endpoint.STATE_EXTENDED` (wired through `request_raw_json` since
  the response shape doesn't match the standard envelope).
- `SmartCastClient.request_raw_json` — low-level helper that returns
  parsed JSON without envelope validation. For SCPL endpoints whose
  response shape diverges from the standard `STATUS`/`ITEMS` wrapper.
  Currently used only by `state_extended`.
- Client-side validation in `set_setting` for list-type settings.
  Sending a value not in the option set now raises
  `VizioInvalidParameterError` with the valid options listed —
  before any HTTP write. Case-insensitive matching also canonicalizes
  the option string (e.g. `'low'` → `'Low'`).
- Range validation in `set_setting` for numeric settings with
  declared bounds. Out-of-range values raise client-side with the
  `min`/`max` reported.
- CLI `--format` is now accepted on every output-producing
  subcommand (`info all`, `input list`, `settings list`, etc.). Leaf
  position wins on conflict with the global `--format`.
- `NUM_0..NUM_9` mapped to `(codeset 0, code 0..9)` in `TV_KEYS`.
  Tuner-equipped TVs use these for direct channel entry; tuner-less
  models reject the keys with `FAILURE` from the device (much better
  UX than `VizioUnsupportedError: not in keymap`).
- WebSocket-not-supported devices now surface as
  `VizioUnsupportedError("device rejected event-register …")` rather
  than the bare `VizioInvalidParameterError`. The message tells
  callers to fall back to polling.

### Changed

- **BREAKING (all releases pre-0.1.0): `set_input` now uses PUT method.**
  Previously it sent the body in a GET request, which the device
  silently ignored and returned success — `set_input` was effectively
  a no-op on real hardware. Now sends a real PUT and the input
  actually changes.
- **BREAKING: `set_input` translates the user's input to the cname
  before sending.** Previously sent the user's literal string, which
  the device rejected with `FAILURE` for display names and
  `HASHVAL_ERROR` for meta_names. Now resolves any of cname / name /
  meta_name (case-insensitive) to the canonical cname.
- `set_input` short-circuits when already on the target input (no PUT
  sent, no exception). Captured live: setting `current_input` to its
  current value returns `FAILURE` from the device — short-circuit
  matches user intent better than propagating that error.
- `Vizio.get_inputs()` now also fetches `current_input` so
  `is_current` is populated correctly across firmware revisions
  (modern firmware doesn't include the synthetic `current_input`
  item in the inputs response).
- `is_current` matching now compares the device's `current_input`
  value against `name` OR `meta_name` OR `cname` (case-insensitive).
  Hardware probing revealed the device's `current_input.VALUE` is
  inconsistent across input types: for `cast` it returns the
  meta_name (`SMARTCAST`); for `hdmi2` it returns the display name
  (`HDMI-2`) even when the input has been user-renamed.
- `Vizio.mute()` and `Vizio.unmute()` are now state-aware: read the
  current mute state, send `MUTE_TOGGLE` only on mismatch. Idempotent
  and works across firmware variants where discrete `MUTE_ON` /
  `MUTE_OFF` codes don't exist.
- `Vizio.get_setting(category, name)` now also fetches the static
  options tree. Returned `SettingInfo` carries populated `options` /
  `min` / `max` fields for the leaf — previously only
  `Vizio.get_settings(category)` (the bulk fetcher) did so. Cost: one
  extra HTTP GET per call.
- `Vizio.get_esn()`, `get_serial_number()`, `get_version()` now
  prefer the aggregate `tv_information` endpoint (one round trip for
  all three fields combined), falling back to per-field endpoints
  on older firmware. Identity is cached per-Vizio-instance after the
  first fetch — identity is immutable for a device's lifetime.
- `get_version()` now looks up both `version` (legacy firmware) and
  `firmware` (modern firmware) cnames in the aggregate response —
  modern firmware exposes the version under `firmware`, not
  `version`.
- `EVENT_REGISTER_BODY` is now `{"REQUEST": "MODIFY", "VALUE": "TRUE"}`.
  Hardware probing revealed `{"REQUEST": "MODIFY"}` alone returns
  `INVALID_PARAMETER` on at least one firmware (3.720.9.1-1); adding
  `VALUE: "TRUE"` makes the same device return `SUCCESS`. The
  string-typed `"TRUE"` matters: `true` (JSON bool) crashes the
  device's parser and returns an HTTP 500 with HTML body.
- HTTP 401/403 from the device now maps to `VizioAuthError` instead
  of `VizioConnectionError`. Hardware-verified: re-pairing with the
  same `device_id` invalidates the previous token, and subsequent
  calls with the old token return raw HTTP 403 (not the SCPL-envelope
  shape with `PAIRING_DENIED`).
- Discovery handler signature updated to a no-op
  `lambda *_, **__: None` to insulate against zeroconf API drift.
  Previously the named-positional signature broke against
  `zeroconf >= 0.130` which calls handlers with keyword arguments.

### Fixed

- Endpoint-fallback logic now triggers on `URI_NOT_FOUND` responses,
  so multi-path endpoints (ESN / serial / version) actually try the
  legacy-firmware path when the modern-firmware path 404s.
- The post-PUT validation in `_check_status` no longer fails for
  `set_input` PUT acknowledgements. The current_input PUT response
  carries only `NAME` and `HASHVAL` (no `CNAME`), so leaving
  `item_cname='current_input'` set on the resolved spec made the
  validator raise `VizioNotFoundError` on the PUT's own success
  response. Fix: clear `item_cname` when overriding spec method to
  PUT.

### Documentation

- Added `tests/captured/` with 17 raw HTTP responses captured live
  from a VHD24M-0810 (firmware 3.720.9.1-1). Fixture-replay tests in
  `tests/test_captured_replay.py` lock the on-the-wire shapes into
  the test suite. PII scrubbed (TV name → `Test TV`, serial →
  `TEST00000000001`, device UUID → zeros).

### Hardware verification

The following protocol-notes items moved from `HARDWARE-VERIFY` to
verified during this release cycle (real-device probe results
captured in fixtures):

- #6 — synthetic `current_input` in inputs response
- #7 — setting types filter (pyvizio's filter is not necessary)
- #9 — `current_app` shape when no app running
- #18 — auth tokens are durable bearer tokens, **invalidated on
  re-pair with same device_id** (immediate, surfaces as HTTP 403)
- #21 — pairing cooldown / cancel behavior
- #28 — WebSocket SCPL: `EVENT_REGISTER_BODY` requires `VALUE:"TRUE"`;
  WS server gating is per-device (some devices accept the register
  but no WS server actually listens)
