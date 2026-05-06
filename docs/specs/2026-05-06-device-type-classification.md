# Device-type classification API

**Status:** approved 2026-05-06
**Branch:** `feat/device-type-classification`
**Tracks:** follow-up to PR #11 (HA prep additions)

## Goal

Provide a layered API for determining a Vizio device's `DeviceType`, so
callers can resolve from a host (or a model string) to one of the five
`DeviceType` enum values. Designed to compose: each layer is independently
useful and reusable.

## Layers

```
Layer 1: TV vs audio                  ← async_is_tv (existing)
Layer 2: Soundbar vs Crave family     ← is_crave_model (new, sync)
Layer 3: Specific Crave variant       ← classify_crave_model (new, sync)
Composer: Layers 1–3 chained          ← async_classify_device (new, async)
```

## Public API

All four functions live in `src/vizaio/discovery.py` and are re-exported
from `vizaio.__init__`.

### `async_is_tv` (existing, unchanged)

```python
async def async_is_tv(
    host: str,
    *,
    port: int | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float | None = None,
) -> bool
```

Auth-asymmetry probe. Already shipped in PR #11. No changes.

### `is_crave_model` (new)

```python
def is_crave_model(model: str) -> bool
```

Pure synchronous. Returns `True` iff `model.upper().startswith("SP")`.
Vizio's Crave product line uses the `SP*-*` model-name pattern
(`SP30-E0`, `SP50-D5`, `SP70-D5`); no other Vizio audio device uses this
prefix. Empty string returns `False`.

### `classify_crave_model` (new)

```python
def classify_crave_model(model: str) -> DeviceType
```

Pure synchronous. Maps a Crave model string to its specific variant.

| Prefix (case-insensitive) | Returns |
|---|---|
| `SP30*` | `DeviceType.CRAVE_GO` |
| `SP50*` | `DeviceType.CRAVE360` |
| `SP70*` | `DeviceType.CRAVE_PRO` |
| Other `SP*` (unknown variant) | `DeviceType.CRAVE_GO` (lenient default) |
| Anything not starting with `SP` | raises `ValueError` |

**Precondition:** caller must verify `is_crave_model(model)` first.
Calling `classify_crave_model` on a TV or soundbar model is a programmer
error and raises.

### `async_classify_device` (new)

```python
async def async_classify_device(
    host: str,
    *,
    port: int | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float | None = None,
) -> DeviceType
```

Composer that walks all three layers:

1. Call `async_is_tv(host, ...)`. If `True`, return `DeviceType.TV`.
2. Construct `Vizio(host, device_type=DeviceType.SOUNDBAR, ...)`
   (matching `async_is_tv`'s pattern — soundbar profile so the
   unauthenticated `Endpoint.DEVICE_INFO` call doesn't trip auth gating).
3. Issue an unauthenticated GET against `Endpoint.DEVICE_INFO` and
   extract `SYSTEM_INFO.MODEL_NAME` from the response. We deliberately
   do **not** use `Vizio.get_model_name()` here — that method delegates
   to `parse_model_name`, which returns the friendly `NAME` field for
   non-TV settings roots (e.g., `"Crave Go"`), not the canonical model
   identifier (`"SP30-E0"`). The Crave-prefix matching needs the model
   identifier.
4. If `is_crave_model(model)` → return `classify_crave_model(model)`.
5. Else → return `DeviceType.SOUNDBAR`.
6. On any `VizioError` during steps 2–4, OR when `SYSTEM_INFO.MODEL_NAME`
   is missing/empty in the response → return `DeviceType.SOUNDBAR` (we
   already established it's not a TV at step 1, so SOUNDBAR is the
   safest default).

## Behavior summary

| Input shape | Output |
|---|---|
| TV host (auth required on settings) | `TV` |
| Soundbar host (no auth, model not SP*) | `SOUNDBAR` |
| Crave host with known model | `CRAVE_GO` / `CRAVE360` / `CRAVE_PRO` |
| Crave host with unknown SP* variant | `CRAVE_GO` |
| Unreachable host (auth probe fails) | `TV` (lenient — matches `async_is_tv`) |
| Audio host but deviceinfo fetch fails | `SOUNDBAR` |

## Non-goals

- Not changing the `DeviceType` enum (already has all 5 values).
- Not modifying `async_is_tv`'s contract.
- Not classifying TV sub-families (V-series vs M-series). The single
  `DeviceType.TV` covers all TV variants — sub-classification is
  irrelevant to profile selection.
- Not introducing new device types beyond what's already in the enum.

## Testing

TDD per the project's standard. New tests in `tests/test_discovery.py`,
three new test classes:

- `TestIsCraveModel` — SP30/SP50/SP70 → True; soundbar prefixes → False;
  TV prefixes → False; case insensitivity; empty string.
- `TestClassifyCraveModel` — three mapping cases (SP30/SP50/SP70 → the
  specific variant), ValueError on non-Crave, lenient fallback for
  unknown SP*.
- `TestAsyncClassifyDevice` — composer paths: TV → TV; soundbar →
  SOUNDBAR; Crave → CRAVE_*; auth probe failure → TV; deviceinfo
  failure post-non-TV → SOUNDBAR.

Async tests mock `Vizio.ping_auth` and patch the underlying client
request for the deviceinfo step (since the composer reads
`SYSTEM_INFO.MODEL_NAME` directly rather than going through
`Vizio.get_model_name()`).

## Verification gates

- All new + existing tests pass
- `ruff check`, `ruff format`, `mypy --strict` clean
- Pre-commit hooks pass (markdownlint, etc.)

## Out of scope (possible future follow-ups)

- A method on `DiscoveredDevice` that delegates to `is_crave_model` +
  `classify_crave_model` for callers with a model already in hand.
- A `vizaio probe <host>` CLI command that prints the full
  classification.
- Using `deviceinfo.SETTINGS_ROOT` as a primary discriminator instead of
  (or in addition to) the auth probe in `async_is_tv`. The current
  auth-probe shape is preserved for 1:1 compatibility with pyvizio's
  HA usage.
