# Device-type classification implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a layered API for classifying a Vizio device into one of
the five `DeviceType` enum values, composing the existing `async_is_tv`
auth-asymmetry probe with sync prefix-based helpers and a deviceinfo
fetch.

**Architecture:** Five new pieces in `src/vizaio/`. One new parse helper
(`parse_system_info_model_name`) extracts the canonical model identifier
from the nested `SYSTEM_INFO.MODEL_NAME` field of the deviceinfo
response. Two pure sync functions (`is_crave_model`,
`classify_crave_model`) live alongside `async_is_tv` in
`src/vizaio/discovery.py`. One async composer
(`async_classify_device`) orchestrates the three layers. All four
classifier functions are re-exported from `vizaio.__init__`.

**Tech Stack:** Python 3.12+, aiohttp, pytest + aioresponses for tests,
ruff/mypy for lint+types.

**Spec:** `docs/specs/2026-05-06-device-type-classification.md`

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/vizaio/parse.py` | Modify | Add `parse_system_info_model_name(response) -> str` extracting the canonical model identifier from `SYSTEM_INFO.MODEL_NAME`. |
| `src/vizaio/discovery.py` | Modify | Add `is_crave_model`, `classify_crave_model`, `async_classify_device`. New imports: `Endpoint`, `parse_system_info_model_name`. |
| `src/vizaio/__init__.py` | Modify | Re-export the three new symbols, add to `__all__`. |
| `tests/test_parse_extra.py` | Modify | Tests for `parse_system_info_model_name` against the existing `device_info.json` capture. |
| `tests/test_discovery.py` | Modify | Three new test classes: `TestIsCraveModel`, `TestClassifyCraveModel`, `TestAsyncClassifyDevice`. |

---

## Task 1: Parse helper for SYSTEM_INFO.MODEL_NAME

**Files:**

- Modify: `src/vizaio/parse.py` (insert after `parse_vizios_binary` at
  line ~333)
- Test: `tests/test_parse_extra.py`

The composer needs the canonical model identifier (`"VHD24M-0810"`,
`"SP30-E0"`), not the friendly NAME. `parse_device_info` only flattens
the top level, so `SYSTEM_INFO.MODEL_NAME` (nested) requires a dedicated
walker. Mirrors the navigation pattern of `parse_vizios_binary`.

- [ ] **Step 1: Read the test fixture to confirm field shape**

```bash
grep -A4 '"SYSTEM_INFO"' tests/captured/device_info.json | head -6
```

Expected output includes `"MODEL_NAME": "VHD24M-0810"` nested under
`SYSTEM_INFO`. Confirms the live capture has the field in that
location.

- [ ] **Step 2: Write the failing tests**

Find the end of `tests/test_parse_extra.py`. Add a new test class. If
the file imports `parse_device_info` or similar, add
`parse_system_info_model_name` to the same import line.

```python
class TestParseSystemInfoModelName:
    """Extracts ``SYSTEM_INFO.MODEL_NAME`` from a deviceinfo response.
    This is the canonical model identifier (e.g., ``"VHD24M-0810"``,
    ``"SP30-E0"``) — distinct from the friendly ``NAME`` field that
    ``parse_model_name`` returns for non-TV settings roots."""

    def test_returns_model_name_from_live_capture(
        self, deviceinfo_response: Response
    ) -> None:
        # Live VHD24M-0810 capture, verified at SYSTEM_INFO.MODEL_NAME.
        assert (
            parse_system_info_model_name(deviceinfo_response)
            == "VHD24M-0810"
        )

    def test_returns_empty_when_response_has_no_items(self) -> None:
        empty = Response.from_json(
            {"ITEMS": [], "STATUS": {"RESULT": "SUCCESS"}}
        )
        assert parse_system_info_model_name(empty) == ""

    def test_returns_empty_when_system_info_missing(self) -> None:
        no_system = Response.from_json(
            {
                "ITEMS": [{"VALUE": {"MODEL_NAME": "x"}, "CNAME": "deviceinfo"}],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        assert parse_system_info_model_name(no_system) == ""

    def test_returns_empty_when_model_name_missing(self) -> None:
        no_model = Response.from_json(
            {
                "ITEMS": [
                    {"VALUE": {"SYSTEM_INFO": {"CHIPSET": 4}}, "CNAME": "deviceinfo"}
                ],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        assert parse_system_info_model_name(no_model) == ""
```

The `deviceinfo_response` fixture already exists in
`tests/test_device_apps.py:42-48` — copy/import it into
`test_parse_extra.py` if not already present, or move the fixture to
`tests/conftest.py` if multiple files now need it.

If the fixture is not yet shared, add this to `tests/conftest.py`:

```python
import json
from pathlib import Path

import pytest

from vizaio.wire import Response


@pytest.fixture
def deviceinfo_response() -> Response:
    """Live deviceinfo capture from a real VHD24M-0810."""
    raw = json.loads(
        (Path(__file__).parent / "captured" / "device_info.json").read_text()
    )
    return Response.from_json(raw)
```

…and remove the duplicate from `test_device_apps.py`.

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_parse_extra.py::TestParseSystemInfoModelName -v
```

Expected: ImportError on `parse_system_info_model_name` OR 4 tests fail
with `AttributeError`/`NameError`.

- [ ] **Step 4: Implement the function**

Insert into `src/vizaio/parse.py` immediately after the
`parse_vizios_binary` function (around line 333). Mirror the navigation
pattern of `parse_vizios_binary`.

```python
def parse_system_info_model_name(response: Response) -> str:
    """
    Extract ``SYSTEM_INFO.MODEL_NAME`` — the canonical model identifier.

    Distinct from :func:`parse_model_name`, which returns the friendly
    ``NAME`` field for non-TV settings roots. Crave-prefix matching
    needs the model identifier (``"SP30-E0"``), not the friendly name
    (``"Crave Go"``).

    Returns the bare string. Empty string when absent — older firmware
    may not expose the nested SYSTEM_INFO block.
    """
    if not response.items:
        return ""
    value = response.items[0].value
    if not isinstance(value, Mapping):
        return ""
    system_info = value.get("system_info")
    if not isinstance(system_info, Mapping):
        return ""
    model_name = system_info.get("model_name")
    if not isinstance(model_name, str):
        return ""
    return model_name
```

The `Mapping` import is already in scope at `parse.py` (used by
`parse_device_info`).

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run python -m pytest tests/test_parse_extra.py::TestParseSystemInfoModelName -v
```

Expected: 4 passed.

- [ ] **Step 6: Run lint, format, mypy**

```bash
uv run ruff check src/vizaio tests && \
  uv run ruff format --check src/vizaio tests && \
  uv run python -m mypy src/vizaio
```

Expected: All checks pass; "Success: no issues found" from mypy.

- [ ] **Step 7: Commit**

```bash
git add src/vizaio/parse.py tests/test_parse_extra.py tests/conftest.py
git commit -m "$(cat <<'EOF'
feat: parse_system_info_model_name parser helper

Extracts the canonical ``SYSTEM_INFO.MODEL_NAME`` from a deviceinfo
response. Distinct from ``parse_model_name`` which returns the
friendly NAME field for non-TV settings roots — Crave-prefix matching
needs the model identifier (``"SP30-E0"``), not the friendly name.
EOF
)"
```

---

## Task 2: `is_crave_model` — sync layer 2

**Files:**

- Modify: `src/vizaio/discovery.py` (insert after `async_is_tv`,
  approximately at the end of file or in its own section)
- Modify: `src/vizaio/__init__.py` (add to imports + `__all__`)
- Test: `tests/test_discovery.py` (insert new class after
  `TestAsyncIsTv`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_discovery.py` immediately after the `TestAsyncIsTv`
class:

```python
class TestIsCraveModel:
    """Pure prefix check for Vizio's Crave product line. Crave models
    follow the ``SP*-*`` pattern (``SP30-E0``, ``SP50-D5``,
    ``SP70-D5``); no other Vizio audio device uses this prefix."""

    def test_sp30_is_crave(self) -> None:
        assert is_crave_model("SP30-E0") is True

    def test_sp50_is_crave(self) -> None:
        assert is_crave_model("SP50-D5") is True

    def test_sp70_is_crave(self) -> None:
        assert is_crave_model("SP70-D5") is True

    def test_lowercase_is_crave(self) -> None:
        assert is_crave_model("sp30-e0") is True

    def test_tv_model_is_not_crave(self) -> None:
        assert is_crave_model("V505-G9") is False

    def test_soundbar_model_is_not_crave(self) -> None:
        # Vizio soundbars use prefixes like SB36*, S5*, M5*.
        assert is_crave_model("SB36514-G6") is False

    def test_empty_string_is_not_crave(self) -> None:
        assert is_crave_model("") is False
```

Update the `from vizaio.discovery import (...)` block in the file to
add `is_crave_model` to the imports.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_discovery.py::TestIsCraveModel -v
```

Expected: ImportError on `is_crave_model` OR all 7 tests fail with
`NameError`.

- [ ] **Step 3: Implement the function**

Insert into `src/vizaio/discovery.py` immediately after
`async_is_tv` (at the end of the file, in a new section):

```python
# ---------------------------------------------------------------------------
# Pure model-string classifiers (layers 2 + 3)
# ---------------------------------------------------------------------------


def is_crave_model(model: str) -> bool:
    """
    Return ``True`` iff ``model`` is a Crave-family identifier.

    Crave models follow the ``SP*-*`` pattern (``SP30-E0``,
    ``SP50-D5``, ``SP70-D5``); no other Vizio audio device uses this
    prefix. Case-insensitive. Empty string returns ``False``.
    """
    return model.upper().startswith("SP")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python -m pytest tests/test_discovery.py::TestIsCraveModel -v
```

Expected: 7 passed.

- [ ] **Step 5: Re-export from `__init__.py`**

In `src/vizaio/__init__.py`, find the line:

```python
from .discovery import async_is_tv
```

and replace with:

```python
from .discovery import async_is_tv, is_crave_model
```

Then add `"is_crave_model"` to the `__all__` list (lowercase entries
sort to the end; `is_crave_model` goes between `fetch_app_catalog` and
the closing bracket — ruff will sort if needed).

- [ ] **Step 6: Run lint, format, mypy**

```bash
uv run ruff check --fix src/vizaio tests && \
  uv run ruff format src/vizaio tests && \
  uv run python -m mypy src/vizaio
```

Expected: All checks pass.

- [ ] **Step 7: Commit**

```bash
git add src/vizaio/discovery.py src/vizaio/__init__.py tests/test_discovery.py
git commit -m "$(cat <<'EOF'
feat: is_crave_model — pure layer-2 classifier

Sync prefix check returning True for Vizio Crave family models
(SP30/SP50/SP70). Re-exported from the top-level package.
EOF
)"
```

---

## Task 3: `classify_crave_model` — sync layer 3

**Files:**

- Modify: `src/vizaio/discovery.py` (insert after `is_crave_model`)
- Modify: `src/vizaio/__init__.py` (add to imports + `__all__`)
- Test: `tests/test_discovery.py` (insert new class after
  `TestIsCraveModel`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_discovery.py` immediately after `TestIsCraveModel`:

```python
class TestClassifyCraveModel:
    """Maps a Crave-family model string to its specific
    ``DeviceType`` variant. Precondition: caller must have verified
    ``is_crave_model(model)`` first."""

    def test_sp30_maps_to_crave_go(self) -> None:
        assert classify_crave_model("SP30-E0") is DeviceType.CRAVE_GO

    def test_sp50_maps_to_crave360(self) -> None:
        assert classify_crave_model("SP50-D5") is DeviceType.CRAVE360

    def test_sp70_maps_to_crave_pro(self) -> None:
        assert classify_crave_model("SP70-D5") is DeviceType.CRAVE_PRO

    def test_lowercase_resolves_correctly(self) -> None:
        assert classify_crave_model("sp50-d5") is DeviceType.CRAVE360

    def test_unknown_sp_variant_falls_back_to_crave_go(self) -> None:
        # Lenient default: unknown SP* models default to the
        # lowest-spec variant. Forecasted-wrong max_volume is safer
        # when guessed low than guessed high.
        assert classify_crave_model("SP99-X1") is DeviceType.CRAVE_GO

    def test_non_crave_model_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"not a Crave"):
            classify_crave_model("V505-G9")

    def test_empty_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"not a Crave"):
            classify_crave_model("")
```

Update the `from vizaio.discovery import (...)` block to add
`classify_crave_model`. The `DeviceType` import should already be in
the file from prior tests; if not, add it from `vizaio`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_discovery.py::TestClassifyCraveModel -v
```

Expected: ImportError on `classify_crave_model` OR 7 failures with
`NameError`/`AttributeError`.

- [ ] **Step 3: Implement the function**

Insert into `src/vizaio/discovery.py` immediately after
`is_crave_model`:

```python
def classify_crave_model(model: str) -> DeviceType:
    """
    Map a Crave-family model string to its specific ``DeviceType``
    variant.

    | Prefix (case-insensitive) | Returns                  |
    |---------------------------|--------------------------|
    | ``SP30*``                 | ``DeviceType.CRAVE_GO``  |
    | ``SP50*``                 | ``DeviceType.CRAVE360``  |
    | ``SP70*``                 | ``DeviceType.CRAVE_PRO`` |
    | other ``SP*``             | ``DeviceType.CRAVE_GO``  |

    **Precondition:** ``is_crave_model(model)`` must return ``True``.
    Raises :class:`ValueError` otherwise — calling this on a TV or
    soundbar model is a programmer error.
    """
    if not is_crave_model(model):
        raise ValueError(f"{model!r} is not a Crave model")
    upper = model.upper()
    if upper.startswith("SP30"):
        return DeviceType.CRAVE_GO
    if upper.startswith("SP50"):
        return DeviceType.CRAVE360
    if upper.startswith("SP70"):
        return DeviceType.CRAVE_PRO
    # Unknown SP* variant: default to lowest-spec.
    return DeviceType.CRAVE_GO
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python -m pytest tests/test_discovery.py::TestClassifyCraveModel -v
```

Expected: 7 passed.

- [ ] **Step 5: Re-export from `__init__.py`**

In `src/vizaio/__init__.py`, update:

```python
from .discovery import async_is_tv, is_crave_model
```

to:

```python
from .discovery import async_is_tv, classify_crave_model, is_crave_model
```

Add `"classify_crave_model"` to the `__all__` list. Run
`uv run ruff check --fix src/vizaio` to let ruff resort `__all__`.

- [ ] **Step 6: Run lint, format, mypy**

```bash
uv run ruff check src/vizaio tests && \
  uv run ruff format --check src/vizaio tests && \
  uv run python -m mypy src/vizaio
```

Expected: All checks pass.

- [ ] **Step 7: Commit**

```bash
git add src/vizaio/discovery.py src/vizaio/__init__.py tests/test_discovery.py
git commit -m "$(cat <<'EOF'
feat: classify_crave_model — pure layer-3 classifier

Sync mapping from a Crave-family model string to its specific
DeviceType variant (CRAVE_GO/CRAVE360/CRAVE_PRO). Lenient default
to CRAVE_GO for unknown SP* variants; raises ValueError on non-Crave
input (precondition: is_crave_model(model)).
EOF
)"
```

---

## Task 4: `async_classify_device` — composer

**Files:**

- Modify: `src/vizaio/discovery.py` (insert after
  `classify_crave_model`)
- Modify: `src/vizaio/__init__.py` (add to imports + `__all__`)
- Test: `tests/test_discovery.py` (insert new class after
  `TestClassifyCraveModel`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_discovery.py` immediately after
`TestClassifyCraveModel`. The pattern mirrors `TestAsyncIsTv` —
patches `Vizio.ping_auth` and the underlying client request.

```python
class TestAsyncClassifyDevice:
    """Composer that walks the three classification layers. Returns
    ``DeviceType.TV`` when the auth probe says TV. Otherwise fetches
    deviceinfo unauthenticated and reads ``SYSTEM_INFO.MODEL_NAME``
    to refine into SOUNDBAR vs CRAVE_*."""

    async def test_tv_short_circuits_at_layer_1(self) -> None:
        # async_is_tv → True means we don't hit deviceinfo at all.
        with patch.object(
            Vizio,
            "ping_auth",
            new=AsyncMock(side_effect=VizioAuthError("REQUIRES_PAIRING")),
        ):
            result = await async_classify_device("1.2.3.4:7345")
        assert result is DeviceType.TV

    async def test_soundbar_path(self) -> None:
        # async_is_tv → False (ping_auth succeeds).
        # Then deviceinfo returns a non-Crave model.
        deviceinfo = Response.from_json(
            {
                "ITEMS": [
                    {
                        "VALUE": {
                            "SYSTEM_INFO": {"MODEL_NAME": "SB36514-G6"}
                        },
                        "CNAME": "deviceinfo",
                    }
                ],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(
                Vizio, "_request", new=AsyncMock(return_value=deviceinfo)
            ),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.SOUNDBAR

    async def test_crave_go_path(self) -> None:
        deviceinfo = Response.from_json(
            {
                "ITEMS": [
                    {
                        "VALUE": {
                            "SYSTEM_INFO": {"MODEL_NAME": "SP30-E0"}
                        },
                        "CNAME": "deviceinfo",
                    }
                ],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(
                Vizio, "_request", new=AsyncMock(return_value=deviceinfo)
            ),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.CRAVE_GO

    async def test_crave_360_path(self) -> None:
        deviceinfo = Response.from_json(
            {
                "ITEMS": [
                    {
                        "VALUE": {
                            "SYSTEM_INFO": {"MODEL_NAME": "SP50-D5"}
                        },
                        "CNAME": "deviceinfo",
                    }
                ],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(
                Vizio, "_request", new=AsyncMock(return_value=deviceinfo)
            ),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.CRAVE360

    async def test_crave_pro_path(self) -> None:
        deviceinfo = Response.from_json(
            {
                "ITEMS": [
                    {
                        "VALUE": {
                            "SYSTEM_INFO": {"MODEL_NAME": "SP70-D5"}
                        },
                        "CNAME": "deviceinfo",
                    }
                ],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(
                Vizio, "_request", new=AsyncMock(return_value=deviceinfo)
            ),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.CRAVE_PRO

    async def test_deviceinfo_fetch_failure_falls_back_to_soundbar(
        self,
    ) -> None:
        # async_is_tv → False, but the deviceinfo fetch raises.
        # Lenient default: SOUNDBAR (we know it's not a TV from layer 1).
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(
                Vizio,
                "_request",
                new=AsyncMock(side_effect=VizioConnectionError("dropped")),
            ),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.SOUNDBAR

    async def test_deviceinfo_with_empty_model_falls_back_to_soundbar(
        self,
    ) -> None:
        # async_is_tv → False, deviceinfo returns but SYSTEM_INFO is missing.
        deviceinfo = Response.from_json(
            {
                "ITEMS": [{"VALUE": {}, "CNAME": "deviceinfo"}],
                "STATUS": {"RESULT": "SUCCESS"},
            }
        )
        with (
            patch.object(Vizio, "ping_auth", new=AsyncMock(return_value=None)),
            patch.object(
                Vizio, "_request", new=AsyncMock(return_value=deviceinfo)
            ),
        ):
            result = await async_classify_device("1.2.3.4:9000")
        assert result is DeviceType.SOUNDBAR
```

Update imports at the top of `test_discovery.py`:

```python
from vizaio import (
    DeviceType,
    DiscoveredDevice,
    Vizio,
    VizioAuthError,
    VizioConnectionError,
)
from vizaio.discovery import (
    async_classify_device,
    async_is_tv,
    classify_crave_model,
    discover,
    discover_ssdp,
    discover_zeroconf,
    is_crave_model,
)
from vizaio.wire import Response
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_discovery.py::TestAsyncClassifyDevice -v
```

Expected: ImportError on `async_classify_device`.

- [ ] **Step 3: Implement the composer**

First, add the imports needed in `src/vizaio/discovery.py`. After the
existing `from .types import DeviceType, DiscoveredDevice` line, also
import:

```python
from .endpoints import Endpoint
from .errors import VizioError
from .parse import parse_system_info_model_name
```

(`VizioError` is already imported; check before adding a duplicate.
`Endpoint` and `parse_system_info_model_name` are new.)

Insert into `src/vizaio/discovery.py` immediately after
`classify_crave_model`:

```python
async def async_classify_device(
    host: str,
    *,
    port: int | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float | None = None,
) -> DeviceType:
    """
    Classify ``host`` into one of the five :class:`DeviceType` values.

    Walks the three classification layers:

    1. :func:`async_is_tv` — TV vs audio via auth-asymmetry probe.
    2. :func:`is_crave_model` — soundbar vs Crave family by
       ``MODEL_NAME`` prefix.
    3. :func:`classify_crave_model` — specific Crave variant.

    Lenient on failure: an unreachable host returns ``DeviceType.TV``
    (matching :func:`async_is_tv`); a reachable audio host whose
    deviceinfo fetch fails returns ``DeviceType.SOUNDBAR`` (we already
    established it's not a TV at step 1).

    ``host`` may include a port (``"1.2.3.4:7345"``) or accept one via
    the ``port`` kwarg — same shape as :func:`async_is_tv`.
    """
    if await async_is_tv(host, port=port, session=session, timeout=timeout):
        return DeviceType.TV
    if port is not None and ":" not in host:
        host = f"{host}:{port}"
    async with Vizio(
        host,
        device_type=DeviceType.SOUNDBAR,
        session=session,
        timeout=timeout,
    ) as device:
        try:
            response = await device._request(Endpoint.DEVICE_INFO)
        except VizioError:
            return DeviceType.SOUNDBAR
        model = parse_system_info_model_name(response)
    if is_crave_model(model):
        return classify_crave_model(model)
    return DeviceType.SOUNDBAR
```

Note: this calls `device._request` (private) directly because we want
the raw `Response` to feed `parse_system_info_model_name` — using the
public `get_model_name` would return the friendly NAME, not the
canonical model identifier. Same package, conventional access; not a
layering violation.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python -m pytest tests/test_discovery.py::TestAsyncClassifyDevice -v
```

Expected: 7 passed.

- [ ] **Step 5: Re-export from `__init__.py`**

Update the discovery import line:

```python
from .discovery import (
    async_classify_device,
    async_is_tv,
    classify_crave_model,
    is_crave_model,
)
```

Add `"async_classify_device"` to `__all__`. Run
`uv run ruff check --fix src/vizaio` to let ruff resort.

- [ ] **Step 6: Run lint, format, mypy**

```bash
uv run ruff check src/vizaio tests && \
  uv run ruff format --check src/vizaio tests && \
  uv run python -m mypy src/vizaio
```

Expected: All checks pass.

- [ ] **Step 7: Commit**

```bash
git add src/vizaio/discovery.py src/vizaio/__init__.py tests/test_discovery.py
git commit -m "$(cat <<'EOF'
feat: async_classify_device — three-layer classifier composer

Composes async_is_tv (layer 1) + deviceinfo fetch + is_crave_model
(layer 2) + classify_crave_model (layer 3) into a single async API
returning the full DeviceType. Lenient on each layer's failure mode:
auth-probe failure → TV (matches async_is_tv); deviceinfo failure
post-non-TV → SOUNDBAR.
EOF
)"
```

---

## Task 5: Final validation + push + PR

**Files:** none (verification only).

- [ ] **Step 1: Run the full test suite**

```bash
uv run python -m pytest --no-header -q
```

Expected: all tests pass (existing + ~21 new tests across Tasks 1-4).

- [ ] **Step 2: Run lint, format, mypy one more time**

```bash
uv run ruff check src/vizaio tests && \
  uv run ruff format --check src/vizaio tests && \
  uv run python -m mypy src/vizaio
```

Expected: All checks pass.

- [ ] **Step 3: Verify spec coverage**

Read `docs/specs/2026-05-06-device-type-classification.md` and check
that every function in the "Public API" section exists, every test in
the "Testing" section is present, and no scope creep.

- [ ] **Step 4: Push the branch**

```bash
git push -u origin feat/device-type-classification
```

- [ ] **Step 5: Open the PR**

```bash
gh pr create --title "feat: layered device-type classification API" --body "$(cat <<'EOF'
## Summary

Follow-up to #11. Adds a layered API for classifying a Vizio device
into one of the five \`DeviceType\` enum values. Three layers, each
independently usable:

- **Layer 1** (existing): \`async_is_tv(host)\` — auth-asymmetry probe.
- **Layer 2** (new): \`is_crave_model(model)\` — sync prefix check
  (\`SP*\`).
- **Layer 3** (new): \`classify_crave_model(model)\` — sync mapping
  to the specific Crave variant (CRAVE_GO/CRAVE360/CRAVE_PRO).
- **Composer** (new): \`async_classify_device(host)\` — orchestrates
  all three.

The composer fetches deviceinfo unauthenticated and reads
\`SYSTEM_INFO.MODEL_NAME\` directly (not via \`get_model_name\`,
which returns the friendly NAME for non-TV settings roots and would
break the SP-prefix match).

Lenient at every layer: auth-probe failure → TV; deviceinfo failure
post-non-TV → SOUNDBAR; unknown SP* variant → CRAVE_GO (lowest-spec
default).

Spec: \`docs/specs/2026-05-06-device-type-classification.md\`

## Test plan

- [x] ~21 new tests across \`tests/test_parse_extra.py\` and
  \`tests/test_discovery.py\`
- [x] Full suite passes
- [x] \`ruff check\`, \`ruff format\`, \`mypy --strict\` clean
- [x] Pre-commit hooks pass

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

---

## Self-Review

**Spec coverage:**

- Layer 1 (`async_is_tv`) — unchanged, no task needed ✓
- Layer 2 (`is_crave_model`) — Task 2 ✓
- Layer 3 (`classify_crave_model`) — Task 3 ✓
- Composer (`async_classify_device`) — Task 4 ✓
- New parse helper for `SYSTEM_INFO.MODEL_NAME` — Task 1 ✓
- Re-exports from `__init__.py` — covered in Tasks 2, 3, 4 ✓
- Test classes (`TestIsCraveModel`, `TestClassifyCraveModel`,
  `TestAsyncClassifyDevice`) — Tasks 2, 3, 4 ✓
- Verification gates (lint, format, mypy, hooks) — every task ✓

**Placeholder scan:** none.

**Type consistency:** `DeviceType` enum values used: `TV`, `SOUNDBAR`,
`CRAVE_GO`, `CRAVE360`, `CRAVE_PRO`. Verified against
`src/vizaio/types.py:33-37`. Function signatures match across plan
sections.
