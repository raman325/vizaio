# Soundbar Wi-Fi Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
  (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
  checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a caller hand Wi-Fi credentials to a Vizio soundbar that is broadcasting its setup
access point, via both the Python SDK and an interactive CLI wizard.

**Architecture:** Mirrors the existing pairing feature exactly — public primitives on `Vizio` plus a
`WifiSetupSession` async context manager written in terms of them, and a `vizaio wifi` CLI group
that mirrors `vizaio pair`. The device is driven through six new `menu_native` settings leaves under
`{root}/network/`. Joining the soundbar's hotspot is explicitly out of scope (an OS-level
operation), as is post-provision verification (the host loses its route to the device).

**Tech Stack:** Python 3.12+, `aiohttp`, `typer` + `rich` (CLI extra), `pytest` with `asyncio_mode =
"auto"`, `aioresponses` for HTTP mocking.

**Spec:** `docs/superpowers/specs/2026-08-28-soundbar-wifi-provisioning-design.md`

---

## Background you need before starting

Read `docs/protocol-notes.md` §32 for the protocol. The short version — every path below is under
`/menu_native/dynamic/{root}/network/` where `{root}` is `audio_settings` for soundbars:

1. `PUT start_ap_search` — `{"REQUEST":"ACTION","HASHVAL":<h>}`
2. `GET wireless_access_points` — returns the scan list
3. `PUT current_access_point` — `{"REQUEST":"MODIFY","VALUE":[{"NAME":"<ssid>"}],"HASHVAL":<h>}`
4. `PUT set_wifi_password` — `{"REQUEST":"MODIFY","VALUE":"<password>","HASHVAL":<h>}`
5. `PUT stop_ap_search` — `{"REQUEST":"ACTION","HASHVAL":<h>}`

Every `<h>` comes from a GET on that same leaf immediately before the PUT.

**Three traps, all discovered from a real hardware capture in issue #40:**

- **Never set `item=` on any of the six new endpoint rows.** A successful PUT response looks like
  `{"HASHVAL":964400715,"NAME":"Current Access Point"}` — it has no `CNAME`. `client._check_status`
  raises `VizioNotFoundError` whenever `spec.item_cname` is set and the response has no matching
  item, so an `item=` would break every write.
- **`wire._lowercase_keys` recurses into lists.** The device sends
  `"VALUE":[{"EM":"WPA2/PSK","NAME":"..."}]`, so by the time parse code sees it, the keys are `em`,
  `name`, `bssid`, `band`, `rssi`. Read lowercase.
- **`current_access_point` takes `NAME` alone.** The `NAME` + `PASSWORD` variant that the Android
  app's change-network path sends did not work on the tested firmware.

Run the whole suite with `uv run pytest`. Run one test with `uv run pytest
tests/test_wifi.py::test_name -v`.

---

## File Structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/vizaio/types.py` | Modify | `AccessPoint` dataclass, `WifiResult` enum, new `SettingType` members, `ResponseStatus.REQUIRES_SYSTEM_PIN` |
| `src/vizaio/errors.py` | Modify | `VizioWifiError` |
| `src/vizaio/client.py` | Modify | Map Wi-Fi result strings to `VizioWifiError` in `_check_status` |
| `src/vizaio/endpoints.py` | Modify | Six `Endpoint` members + table rows |
| `src/vizaio/_payloads.py` | Modify | `select_access_point`, `join_hidden_network` |
| `src/vizaio/parse.py` | Modify | `parse_access_points`, `parse_current_access_point` |
| `src/vizaio/_device.py` | Modify | Five primitives, `wifi_setup_session()`, `WifiSetupSession` |
| `src/vizaio/__init__.py` | Modify | Public exports |
| `src/vizaio/cli/__init__.py` | Modify | `vizaio wifi` command group |
| `tests/test_wifi.py` | Create | Types, payloads, parse, primitives, session |
| `tests/test_cli_wifi.py` | Create | CLI scan / join / interactive |
| `tests/captured/network_*.json` | Create | Real device payloads from issue #40 |
| `tests/test_captured_replay.py` | Modify | Replay assertions for the new fixtures |
| `tests/test_endpoints.py` | Modify | Row resolution assertions |
| `docs/protocol-notes.md` | Modify | §32 upgraded to hardware-verified |
| `README.md` | Modify | Provisioning example |

---

### Task 1: `AccessPoint` type

**Files:**

- Modify: `src/vizaio/types.py`
- Test: `tests/test_wifi.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_wifi.py`:

```python
"""Soundbar Wi-Fi provisioning — types, payloads, parsing, device API, session."""

from __future__ import annotations

import pytest

from vizaio.types import AccessPoint


def _ap(security: str) -> AccessPoint:
    """Build an AccessPoint varying only the security string."""
    return AccessPoint(
        ssid="net", bssid="aa:bb", security=security, band="2.4", rssi=50
    )


@pytest.mark.parametrize(
    ("security", "expected_open"),
    [
        ("NONE", True),
        ("WEP/NONE", True),
        ("WPA2/PSK", False),
        ("WPA/PSK", False),
        ("WEP", False),
        ("EAP", False),
        ("", True),
    ],
)
def test_access_point_is_open(security: str, expected_open: bool) -> None:
    assert _ap(security).is_open is expected_open


def test_access_point_is_frozen() -> None:
    with pytest.raises(AttributeError):
        _ap("NONE").ssid = "other"  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: FAIL with `ImportError: cannot import name 'AccessPoint' from 'vizaio.types'`

- [ ] **Step 3: Write minimal implementation**

In `src/vizaio/types.py`, add near the other dataclasses (after `InputInfo`):

```python
_SECURITY_MODES: Final[tuple[str, ...]] = ("WEP", "PSK", "EAP", "WPA", "WPA2")


@dataclass(frozen=True, slots=True)
class AccessPoint:
    """
    One Wi-Fi network as reported by a device's ``wireless_access_points``
    scan, or its ``current_access_point``.

    Field names map to the device's keys: ``ssid`` is ``NAME``,
    ``security`` is ``EM``. ``rssi`` is the device's own 0-100 scale, not
    dBm — captured values run 45-70.
    """

    ssid: str
    bssid: str
    security: str
    band: str
    """``"2.4"`` or ``"5"``, as a string — the device sends it that way."""

    rssi: int

    @property
    def is_open(self) -> bool:
        """
        ``True`` when the network needs no password.

        Ports ``VZAccessPointItem.isSecure()`` from the official app: the
        network counts as secured only if ``EM`` names one of the known
        suites *and* is not the literal ``WEP/NONE`` sentinel, which the
        app treats as open despite naming WEP.
        """
        em = self.security.upper()
        if any(mode in em for mode in _SECURITY_MODES):
            return "WEP/NONE" in em
        return True
```

Add `Final` to the `typing` import at the top of the file if it is not already imported.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/types.py tests/test_wifi.py
git commit -m "feat: add AccessPoint type for Wi-Fi scan results"
```

---

### Task 2: `WifiResult`, `ResponseStatus.REQUIRES_SYSTEM_PIN`, new `SettingType` members

**Files:**

- Modify: `src/vizaio/types.py`
- Test: `tests/test_wifi.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_wifi.py`:

```python
from vizaio.types import ResponseStatus, SettingType, WifiResult


def test_wifi_result_values_are_lowercase() -> None:
    # wire._parse_status lowercases before lookup; these must match that.
    for member in WifiResult:
        assert member.value == member.value.lower()


def test_wifi_result_covers_the_app_vocabulary() -> None:
    assert WifiResult("net_wifi_already_connected") is WifiResult.ALREADY_CONNECTED
    assert WifiResult("net_wifi_auth_rejected") is WifiResult.AUTH_REJECTED
    assert WifiResult("net_ip_dhcp_failed") is WifiResult.DHCP_FAILED


def test_requires_system_pin_is_a_response_status() -> None:
    assert ResponseStatus("requires_system_pin") is ResponseStatus.REQUIRES_SYSTEM_PIN


def test_network_setting_types_are_modelled() -> None:
    assert SettingType("T_APS_V1") is SettingType.ACCESS_POINTS
    assert SettingType("T_AP_V1") is SettingType.ACCESS_POINT
    assert SettingType("T_STRING_V1") is SettingType.STRING
    assert SettingType("T_TEST_CONNECTION_V1") is SettingType.TEST_CONNECTION
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: FAIL with `ImportError: cannot import name 'WifiResult' from 'vizaio.types'`

- [ ] **Step 3: Write minimal implementation**

In `src/vizaio/types.py`, add to `SettingType`:

```python
    STRING = "T_STRING_V1"
    """A free-text setting (e.g. ``network/set_wifi_password``)."""

    ACCESS_POINTS = "T_APS_V1"
    """A Wi-Fi scan list. VALUE is a list of access-point objects, not a
    scalar — read it with :func:`vizaio.parse.parse_access_points`."""

    ACCESS_POINT = "T_AP_V1"
    """A single Wi-Fi network (``network/current_access_point``). VALUE is
    a one-element list."""

    TEST_CONNECTION = "T_TEST_CONNECTION_V1"
    """Connection-test results. Documented in protocol-notes §32 but
    deliberately not surfaced through the API — see the spec."""
```

Add to `ResponseStatus`, immediately after `PAIRING_DENIED`:

```python
    REQUIRES_SYSTEM_PIN = "requires_system_pin"
    """Device is PIN-locked and refuses the write until the PIN is
    supplied. Mapped to :class:`VizioAuthError`. Seen on the Wi-Fi
    provisioning leaves; the app's constant is
    ``RESPONSE_REQUIRES_SYSTEM_PIN``, i.e. it belongs to the same family
    as ``REQUIRES_PAIRING`` rather than to the ``NET_*`` radio codes."""
```

Add a new enum after `ResponseStatus`:

```python
class WifiResult(StrEnum):
    """
    Radio/DHCP outcomes the device reports in ``STATUS.RESULT`` on the
    Wi-Fi provisioning leaves.

    Distinct from :class:`ResponseStatus`, which covers protocol
    outcomes. Sourced from ``Constants.VZConnectionConstants`` in the
    official app; none were observed on the wire during hardware
    verification, which returned ``SUCCESS`` at every step.
    """

    ALREADY_CONNECTED = "net_wifi_already_connected"
    """Device is already on the requested SSID. Not an error — the
    official app treats it as success."""

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
    """Device said something in the ``NET_*`` family we don't model. The
    raw string is preserved on :attr:`vizaio.VizioWifiError.code`."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/types.py tests/test_wifi.py
git commit -m "feat: model Wi-Fi result codes and network setting types"
```

---

### Task 3: `VizioWifiError` and the status mapping

**Files:**

- Modify: `src/vizaio/errors.py`
- Modify: `src/vizaio/client.py:357-394` (`_check_status`)
- Test: `tests/test_wifi.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_wifi.py`:

```python
from vizaio.client import _check_status
from vizaio.endpoints import EndpointSpec
from vizaio.errors import VizioAuthError, VizioWifiError
from vizaio.types import AuthRequirement
from vizaio.wire import Response

_SPEC = EndpointSpec(paths=("/x",), method="PUT", auth=AuthRequirement.NONE)


def _status_response(result: str) -> Response:
    """Build a Response carrying an arbitrary STATUS.RESULT string."""
    return Response.from_json({"STATUS": {"RESULT": result, "DETAIL": "d"}})


def test_wifi_result_maps_to_wifi_error() -> None:
    with pytest.raises(VizioWifiError) as excinfo:
        _check_status(_status_response("NET_WIFI_AUTH_REJECTED"), _SPEC)
    assert excinfo.value.result is WifiResult.AUTH_REJECTED
    assert excinfo.value.code == "NET_WIFI_AUTH_REJECTED"


def test_already_connected_still_raises_at_the_transport_layer() -> None:
    # The tolerance for ALREADY_CONNECTED lives in join_access_point, not
    # here — the transport layer has no idea which flow it is serving.
    with pytest.raises(VizioWifiError) as excinfo:
        _check_status(_status_response("NET_WIFI_ALREADY_CONNECTED"), _SPEC)
    assert excinfo.value.result is WifiResult.ALREADY_CONNECTED


def test_unmodelled_net_code_maps_to_unknown_but_keeps_raw() -> None:
    with pytest.raises(VizioWifiError) as excinfo:
        _check_status(_status_response("NET_WIFI_SOMETHING_NEW"), _SPEC)
    assert excinfo.value.result is WifiResult.UNKNOWN
    assert excinfo.value.code == "NET_WIFI_SOMETHING_NEW"


def test_requires_system_pin_maps_to_auth_error() -> None:
    with pytest.raises(VizioAuthError):
        _check_status(_status_response("REQUIRES_SYSTEM_PIN"), _SPEC)


def test_wifi_error_is_a_vizio_error() -> None:
    from vizaio.errors import VizioError

    assert issubclass(VizioWifiError, VizioError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: FAIL with `ImportError: cannot import name 'VizioWifiError' from 'vizaio.errors'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/vizaio/errors.py`:

```python
class VizioWifiError(VizioError):
    """
    A Wi-Fi provisioning leaf reported a radio or DHCP failure.

    Carries the parsed :class:`vizaio.types.WifiResult` so callers can
    branch — an interactive client re-prompts for the password on
    ``AUTH_REJECTED`` rather than aborting — plus the raw device string
    for codes we don't model.
    """

    def __init__(self, result: WifiResult, code: str, detail: str = "") -> None:
        """Store the parsed result and the device's original string."""
        self.result = result
        self.code = code
        self.detail = detail
        super().__init__(f"{code}{f': {detail}' if detail else ''}")
```

Add at the top of `src/vizaio/errors.py`, under `from __future__ import annotations`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import WifiResult
```

In `src/vizaio/client.py`, add near the other module-level constants:

```python
_WIFI_RESULT_VALUES: Final[set[str]] = {r.value for r in WifiResult}
_WIFI_RESULT_PREFIXES: Final[tuple[str, ...]] = ("net_wifi_", "net_ip_", "net_unknown")
```

Import `WifiResult` and `VizioWifiError` in `client.py`.

In `_check_status`, insert immediately before the final `raise VizioResponseError(...)`:

```python
    if status is ResponseStatus.REQUIRES_SYSTEM_PIN:
        raise VizioAuthError(response.detail or status.value)
    lowered = response.result_raw.lower()
    if lowered in _WIFI_RESULT_VALUES or lowered.startswith(_WIFI_RESULT_PREFIXES):
        raise VizioWifiError(
            result=(
                WifiResult(lowered)
                if lowered in _WIFI_RESULT_VALUES
                else WifiResult.UNKNOWN
            ),
            code=response.result_raw,
            detail=response.detail,
        )
```

Note the ordering: `REQUIRES_SYSTEM_PIN` is a real `ResponseStatus` member so it must be matched
before the generic fallback, and the Wi-Fi prefix check catches `NET_*` strings that `_parse_status`
resolved to `ResponseStatus.UNKNOWN`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_wifi.py tests/test_client.py -v`
Expected: PASS, with no regressions in `test_client.py`

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/errors.py src/vizaio/client.py tests/test_wifi.py
git commit -m "feat: raise VizioWifiError for NET_* device results"
```

---

### Task 4: Endpoint rows

**Files:**

- Modify: `src/vizaio/endpoints.py`
- Test: `tests/test_endpoints.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_endpoints.py`:

```python
import pytest

from vizaio.endpoints import Endpoint, resolve
from vizaio.types import AuthRequirement, DeviceType


@pytest.mark.parametrize(
    ("endpoint", "leaf"),
    [
        (Endpoint.AP_SCAN_START, "start_ap_search"),
        (Endpoint.AP_SCAN_STOP, "stop_ap_search"),
        (Endpoint.ACCESS_POINTS, "wireless_access_points"),
        (Endpoint.CURRENT_ACCESS_POINT, "current_access_point"),
        (Endpoint.WIFI_PASSWORD, "set_wifi_password"),
        (Endpoint.HIDDEN_NETWORK, "hidden_network"),
    ],
)
def test_network_endpoints_resolve_under_the_audio_root(
    endpoint: Endpoint, leaf: str
) -> None:
    spec = resolve(endpoint, DeviceType.SOUNDBAR.profile)
    assert spec.paths == (f"/menu_native/dynamic/audio_settings/network/{leaf}",)
    assert spec.method == "GET"


def test_network_endpoints_never_declare_an_item_cname() -> None:
    # A successful PUT returns ITEMS without a CNAME, so an item_cname
    # would make _check_status raise VizioNotFoundError on every write.
    for endpoint in (
        Endpoint.AP_SCAN_START,
        Endpoint.AP_SCAN_STOP,
        Endpoint.ACCESS_POINTS,
        Endpoint.CURRENT_ACCESS_POINT,
        Endpoint.WIFI_PASSWORD,
        Endpoint.HIDDEN_NETWORK,
    ):
        assert resolve(endpoint, DeviceType.SOUNDBAR.profile).item_cname is None


def test_network_endpoints_follow_the_profile_auth_model() -> None:
    assert (
        resolve(Endpoint.ACCESS_POINTS, DeviceType.SOUNDBAR.profile).auth
        is AuthRequirement.NONE
    )
    assert (
        resolve(Endpoint.ACCESS_POINTS, DeviceType.TV.profile).auth
        is AuthRequirement.REQUIRED
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_endpoints.py -v`
Expected: FAIL with `AttributeError: AP_SCAN_START`

- [ ] **Step 3: Write minimal implementation**

In `src/vizaio/endpoints.py`, add to the `Endpoint` enum after the `SETTINGS_OPTIONS` member:

```python
    # Wi-Fi provisioning (settings-tree leaves under {root}/network/)
    AP_SCAN_START = "ap_scan_start"
    AP_SCAN_STOP = "ap_scan_stop"
    ACCESS_POINTS = "access_points"
    CURRENT_ACCESS_POINT = "current_access_point"
    WIFI_PASSWORD = "wifi_password"
    HIDDEN_NETWORK = "hidden_network"
```

Add to the `ENDPOINTS` table, after the `Endpoint.SETTINGS_OPTIONS` row:

```python
    # Wi-Fi provisioning ——————————————————————————————————————————————
    # Declared GET: every write on these leaves is preceded by a hashval
    # fetch on the same path, and the PUT reuses the resolved spec via
    # ``replace(spec, method="PUT")``.
    #
    # None of these set ``item=``. A successful PUT returns
    # ``ITEMS: [{"HASHVAL": ..., "NAME": "Current Access Point"}]`` with
    # no CNAME, so an item_cname would make ``_check_status`` raise
    # VizioNotFoundError on every successful write. Verified against a
    # real soundbar capture (issue #40).
    Endpoint.AP_SCAN_START: row("GET", "{root}/network/start_ap_search"),
    Endpoint.AP_SCAN_STOP: row("GET", "{root}/network/stop_ap_search"),
    Endpoint.ACCESS_POINTS: row("GET", "{root}/network/wireless_access_points"),
    Endpoint.CURRENT_ACCESS_POINT: row("GET", "{root}/network/current_access_point"),
    Endpoint.WIFI_PASSWORD: row("GET", "{root}/network/set_wifi_password"),
    Endpoint.HIDDEN_NETWORK: row("GET", "{root}/network/hidden_network"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_endpoints.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/endpoints.py tests/test_endpoints.py
git commit -m "feat: catalog the network provisioning endpoints"
```

---

### Task 5: Payload builders

**Files:**

- Modify: `src/vizaio/_payloads.py`
- Test: `tests/test_payloads.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_payloads.py`:

```python
from vizaio._payloads import join_hidden_network, select_access_point


def test_select_access_point_sends_name_only() -> None:
    # Verified on hardware (issue #40): the NAME+PASSWORD variant the
    # Android app's change-network path sends did NOT work; NAME alone did.
    assert select_access_point(ssid="MinasTirith", hashval=3250072061) == {
        "REQUEST": "MODIFY",
        "VALUE": [{"NAME": "MinasTirith"}],
        "HASHVAL": 3250072061,
    }


def test_join_hidden_network_sends_name_and_password() -> None:
    assert join_hidden_network(ssid="ghost", password="pw", hashval=7) == {
        "REQUEST": "MODIFY",
        "VALUE": [{"NAME": "ghost", "PASSWORD": "pw"}],
        "HASHVAL": 7,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_payloads.py -v`
Expected: FAIL with `ImportError: cannot import name 'select_access_point'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/vizaio/_payloads.py`:

```python
def select_access_point(*, ssid: str, hashval: int) -> dict[str, Any]:
    """
    Point the device at a visible network by SSID.

    ``VALUE`` is a one-element list carrying ``NAME`` only. The official
    app's change-network path also sends ``PASSWORD`` here, but that
    variant was rejected by the firmware tested in issue #40 while this
    one succeeded. The password goes in a separate PUT — see
    :func:`write_setting` against ``network/set_wifi_password``.
    """
    return {"REQUEST": "MODIFY", "VALUE": [{"NAME": ssid}], "HASHVAL": hashval}


def join_hidden_network(*, ssid: str, password: str, hashval: int) -> dict[str, Any]:
    """
    Join a network that does not broadcast its SSID.

    Unlike the visible path this carries the password in the same PUT.
    APK-derived and **not hardware verified** — no hidden network was
    available during the issue #40 capture.
    """
    return {
        "REQUEST": "MODIFY",
        "VALUE": [{"NAME": ssid, "PASSWORD": password}],
        "HASHVAL": hashval,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_payloads.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/_payloads.py tests/test_payloads.py
git commit -m "feat: add access-point selection payload builders"
```

---

### Task 6: Parsers

**Files:**

- Modify: `src/vizaio/parse.py`
- Test: `tests/test_wifi.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_wifi.py`:

```python
from vizaio.parse import parse_access_points, parse_current_access_point


def _aps_response() -> Response:
    """The real three-AP payload from issue #40, SSIDs redacted."""
    return Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {
                    "HASHVAL": 203850784,
                    "CNAME": "wireless_access_points",
                    "TYPE": "T_APS_V1",
                    "NAME": "Wireless Access Points",
                    "VALUE": [
                        {
                            "EM": "WPA2/PSK",
                            "RSSI": 65,
                            "NAME": "net-5g",
                            "BSSID": "aa:bb:cc",
                            "BAND": "5",
                        },
                        {
                            "EM": "WPA2/PSK",
                            "RSSI": 70,
                            "NAME": "net-24",
                            "BSSID": "dd:ee:ff",
                            "BAND": "2.4",
                        },
                    ],
                }
            ],
        }
    )


def test_parse_access_points_reads_the_scan_list() -> None:
    aps = parse_access_points(_aps_response())
    assert [a.ssid for a in aps] == ["net-5g", "net-24"]
    assert aps[0].band == "5"
    assert aps[0].rssi == 65
    assert aps[0].security == "WPA2/PSK"
    assert aps[0].bssid == "aa:bb:cc"
    assert aps[0].is_open is False


def test_parse_access_points_empty_when_item_absent() -> None:
    response = Response.from_json({"STATUS": {"RESULT": "SUCCESS", "DETAIL": ""}})
    assert parse_access_points(response) == ()


def test_parse_current_access_point_returns_none_when_unconfigured() -> None:
    # Real sentinel from issue #40: empty NAME, zeroed BSSID.
    response = Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {
                    "HASHVAL": 3250072061,
                    "CNAME": "current_access_point",
                    "TYPE": "T_AP_V1",
                    "NAME": "Current Access Point",
                    "VALUE": [
                        {
                            "EM": "NONE",
                            "RSSI": 0,
                            "NAME": "",
                            "BSSID": "000000-000000",
                            "BAND": "2.4",
                        }
                    ],
                }
            ],
        }
    )
    assert parse_current_access_point(response) is None


def test_parse_current_access_point_returns_the_joined_network() -> None:
    response = Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {
                    "CNAME": "current_access_point",
                    "TYPE": "T_AP_V1",
                    "NAME": "Current Access Point",
                    "VALUE": [
                        {
                            "EM": "WPA2/PSK",
                            "RSSI": 62,
                            "NAME": "joined",
                            "BSSID": "aa:bb",
                            "BAND": "5",
                        }
                    ],
                }
            ],
        }
    )
    ap = parse_current_access_point(response)
    assert ap is not None
    assert ap.ssid == "joined"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_access_points'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/vizaio/parse.py` (it already imports `Mapping` from `collections.abc`; add
`AccessPoint` to the `.types` import):

```python
def _access_point(raw: Mapping[str, Any]) -> AccessPoint:
    """
    Build an :class:`AccessPoint` from one already-lowercased VALUE entry.

    Keys are lowercase because :func:`vizaio.wire._lowercase_keys`
    recurses into lists as well as dicts.
    """
    rssi_raw = raw.get("rssi")
    try:
        rssi = int(rssi_raw) if rssi_raw is not None else 0
    except (TypeError, ValueError):
        rssi = 0
    return AccessPoint(
        ssid=str(raw.get("name", "")),
        bssid=str(raw.get("bssid", "")),
        security=str(raw.get("em", "")),
        band=str(raw.get("band", "")),
        rssi=rssi,
    )


def parse_access_points(response: Response) -> tuple[AccessPoint, ...]:
    """
    Extract the scan list from a ``wireless_access_points`` response.

    Returns an empty tuple when the item is missing or the scan has not
    yet produced results — an empty list is a normal early-scan state,
    not an error.
    """
    item = response.find_item("wireless_access_points")
    if item is None or not isinstance(item.value, list):
        return ()
    return tuple(
        _access_point(entry) for entry in item.value if isinstance(entry, Mapping)
    )


def parse_current_access_point(response: Response) -> AccessPoint | None:
    """
    Extract the joined network from a ``current_access_point`` response.

    Returns ``None`` when the device is not on a network. An
    unconfigured device reports a sentinel entry rather than an empty
    list — empty ``NAME`` with ``BSSID`` of ``000000-000000`` (captured
    in issue #40) — so the empty SSID is the real signal.
    """
    item = response.find_item("current_access_point")
    if item is None or not isinstance(item.value, list) or not item.value:
        return None
    first = item.value[0]
    if not isinstance(first, Mapping):
        return None
    access_point = _access_point(first)
    return access_point if access_point.ssid else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/parse.py tests/test_wifi.py
git commit -m "feat: parse access-point scan results"
```

---

### Task 7: Device read primitives and scan control

**Files:**

- Modify: `src/vizaio/_device.py`
- Test: `tests/test_wifi.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_wifi.py`:

```python
from typing import Any
from unittest.mock import AsyncMock

from vizaio import DeviceType, Vizio
from vizaio.errors import VizioResponseError


def _soundbar(client: Any) -> Vizio:
    """Build a Vizio bound to a stub client, bypassing HTTP entirely."""
    device = Vizio(host="192.168.1.101:9000", device_type=DeviceType.SOUNDBAR)
    device._client = client  # noqa: SLF001
    return device


def _leaf_response(cname: str, hashval: int, value: Any = "") -> Response:
    """Build a single-item GET response for a settings leaf."""
    return Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {"CNAME": cname, "TYPE": "T_ACTION_V1", "NAME": cname, "VALUE": value,
                 "HASHVAL": hashval}
            ],
        }
    )


async def test_start_ap_scan_fires_an_action_with_the_fetched_hashval() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("start_ap_search", 300381621, "T_ACTION_V1"),
        Response.from_json({"STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"}}),
    ]
    await _soundbar(client).start_ap_scan()

    get_call, put_call = client.request_spec.call_args_list
    assert get_call.args[0].method == "GET"
    assert put_call.args[0].method == "PUT"
    assert put_call.kwargs["body"] == {"REQUEST": "ACTION", "HASHVAL": 300381621}


async def test_stop_ap_scan_fires_an_action() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("stop_ap_search", 139197155, "T_ACTION_V1"),
        Response.from_json({"STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"}}),
    ]
    await _soundbar(client).stop_ap_scan()
    assert client.request_spec.call_args_list[1].kwargs["body"] == {
        "REQUEST": "ACTION",
        "HASHVAL": 139197155,
    }


async def test_get_access_points_returns_parsed_networks() -> None:
    client = AsyncMock()
    client.request_spec.return_value = _aps_response()
    aps = await _soundbar(client).get_access_points()
    assert [a.ssid for a in aps] == ["net-5g", "net-24"]


async def test_get_current_access_point_returns_none_when_unset() -> None:
    client = AsyncMock()
    client.request_spec.return_value = Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [
                {
                    "CNAME": "current_access_point",
                    "TYPE": "T_AP_V1",
                    "NAME": "Current Access Point",
                    "VALUE": [{"EM": "NONE", "RSSI": 0, "NAME": "",
                               "BSSID": "000000-000000", "BAND": "2.4"}],
                }
            ],
        }
    )
    assert await _soundbar(client).get_current_access_point() is None


async def test_missing_hashval_raises_response_error() -> None:
    client = AsyncMock()
    client.request_spec.return_value = Response.from_json(
        {
            "STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"},
            "ITEMS": [{"CNAME": "start_ap_search", "TYPE": "T_ACTION_V1",
                       "NAME": "Start AP Search", "VALUE": "T_ACTION_V1"}],
        }
    )
    with pytest.raises(VizioResponseError, match="HASHVAL"):
        await _soundbar(client).start_ap_scan()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: FAIL with `AttributeError: 'Vizio' object has no attribute 'start_ap_scan'`

- [ ] **Step 3: Write minimal implementation**

In `src/vizaio/_device.py`, add to the imports: `parse_access_points`, `parse_current_access_point`
from `.parse`, and `AccessPoint` from `.types`.

Add these methods to `Vizio`, after the settings-action methods:

```python
    # -----------------------------------------------------------------
    # Wi-Fi provisioning — see docs/protocol-notes.md §32
    # -----------------------------------------------------------------

    async def start_ap_scan(self) -> None:
        """
        Ask the device to scan for nearby Wi-Fi networks.

        Fire-and-forget: results accumulate in the scan list over the
        following seconds. Poll :meth:`get_access_points` to read them.
        Always pair with :meth:`stop_ap_scan`, or use
        :meth:`wifi_setup_session` which does that for you.
        """
        await self._put_network_leaf(
            Endpoint.AP_SCAN_START, lambda hashval: _payloads.action_setting(hashval)
        )

    async def stop_ap_scan(self) -> None:
        """Stop a scan started by :meth:`start_ap_scan`."""
        await self._put_network_leaf(
            Endpoint.AP_SCAN_STOP, lambda hashval: _payloads.action_setting(hashval)
        )

    async def get_access_points(self) -> tuple[AccessPoint, ...]:
        """
        Return the networks the device can currently see.

        Empty until a scan started by :meth:`start_ap_scan` produces
        results; an empty tuple is a normal early state, not an error.
        """
        return parse_access_points(await self._request(Endpoint.ACCESS_POINTS))

    async def get_current_access_point(self) -> AccessPoint | None:
        """Return the network the device is on, or ``None`` if unconfigured."""
        return parse_current_access_point(
            await self._request(Endpoint.CURRENT_ACCESS_POINT)
        )

    async def _network_hashval(self, endpoint: Endpoint) -> int:
        """GET a network leaf and return the HASHVAL its write must echo."""
        response = await self._request(endpoint)
        item = response.items[0] if response.items else None
        if item is None or item.hashval is None:
            raise VizioResponseError(f"{endpoint.value} returned no HASHVAL to echo")
        return item.hashval

    async def _put_network_leaf(
        self, endpoint: Endpoint, body_for: Callable[[int], dict[str, Any]]
    ) -> None:
        """
        GET a network leaf for its hashval, then PUT, retrying once.

        Same stale-hashval contract as :meth:`set_setting` — see
        protocol-notes §13. ``body_for`` is called again on retry so the
        fresh hashval lands in the rebuilt body.
        """
        try:
            await self._put_network_body(
                endpoint, body_for(await self._network_hashval(endpoint))
            )
        except VizioInvalidParameterError:
            await self._put_network_body(
                endpoint, body_for(await self._network_hashval(endpoint))
            )

    async def _put_network_body(
        self, endpoint: Endpoint, body: dict[str, Any]
    ) -> None:
        """PUT ``body`` at a network leaf whose row is declared GET."""
        spec = replace(resolve(endpoint, self._profile), method="PUT")
        self._check_auth(spec.auth)
        await self._client.request_spec(spec, body=body)
```

`Callable` needs importing from `collections.abc` if it is not already imported in `_device.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/_device.py tests/test_wifi.py
git commit -m "feat: add Wi-Fi scan control and access-point reads"
```

---

### Task 8: `join_access_point`

**Files:**

- Modify: `src/vizaio/_device.py`
- Test: `tests/test_wifi.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_wifi.py`:

```python
def _ok() -> Response:
    """A bare SUCCESS envelope, as returned by a settings PUT."""
    return Response.from_json({"STATUS": {"RESULT": "SUCCESS", "DETAIL": "Success"}})


async def test_join_access_point_selects_then_sets_password() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("current_access_point", 3250072061),
        _ok(),
        _leaf_response("set_wifi_password", 2698362233),
        _ok(),
    ]
    await _soundbar(client).join_access_point("MinasTirith", password="s3cret")

    calls = client.request_spec.call_args_list
    assert calls[1].kwargs["body"] == {
        "REQUEST": "MODIFY",
        "VALUE": [{"NAME": "MinasTirith"}],
        "HASHVAL": 3250072061,
    }
    assert calls[3].kwargs["body"] == {
        "REQUEST": "MODIFY",
        "VALUE": "s3cret",
        "HASHVAL": 2698362233,
    }
    # Ordering matters: selection must land before the password.
    assert calls[1].args[0].paths[0].endswith("/current_access_point")
    assert calls[3].args[0].paths[0].endswith("/set_wifi_password")


async def test_join_access_point_sends_empty_password_when_omitted() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("current_access_point", 1),
        _ok(),
        _leaf_response("set_wifi_password", 2),
        _ok(),
    ]
    await _soundbar(client).join_access_point("open-net")
    assert client.request_spec.call_args_list[3].kwargs["body"]["VALUE"] == ""


async def test_join_access_point_hidden_uses_one_put() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [_leaf_response("hidden_network", 99), _ok()]
    await _soundbar(client).join_access_point("ghost", password="pw", hidden=True)

    assert client.request_spec.call_count == 2
    assert client.request_spec.call_args_list[1].kwargs["body"] == {
        "REQUEST": "MODIFY",
        "VALUE": [{"NAME": "ghost", "PASSWORD": "pw"}],
        "HASHVAL": 99,
    }


async def test_join_access_point_tolerates_already_connected() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("current_access_point", 1),
        VizioWifiError(WifiResult.ALREADY_CONNECTED, "NET_WIFI_ALREADY_CONNECTED"),
    ]
    # Must not raise: the device is already where we wanted it.
    await _soundbar(client).join_access_point("MinasTirith", password="pw")


async def test_join_access_point_propagates_auth_rejection() -> None:
    client = AsyncMock()
    client.request_spec.side_effect = [
        _leaf_response("current_access_point", 1),
        _ok(),
        _leaf_response("set_wifi_password", 2),
        VizioWifiError(WifiResult.AUTH_REJECTED, "NET_WIFI_AUTH_REJECTED"),
    ]
    with pytest.raises(VizioWifiError) as excinfo:
        await _soundbar(client).join_access_point("MinasTirith", password="wrong")
    assert excinfo.value.result is WifiResult.AUTH_REJECTED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: FAIL with `AttributeError: 'Vizio' object has no attribute 'join_access_point'`

- [ ] **Step 3: Write minimal implementation**

Add to `Vizio` in `src/vizaio/_device.py`, and import `VizioWifiError` and `WifiResult`:

```python
    async def join_access_point(
        self,
        ssid: str,
        *,
        password: str | None = None,
        hidden: bool = False,
    ) -> None:
        """
        Hand the device Wi-Fi credentials.

        For a visible network this is two writes — select the SSID, then
        set the password — because the firmware tested in issue #40
        rejected the combined form. A hidden network takes one write
        carrying both (APK-derived, **not hardware verified**).

        ``password=None`` sends an empty string, matching the official
        app, which performs the password step even for open networks.

        Returns normally on ``NET_WIFI_ALREADY_CONNECTED``: the device is
        already on the requested network, which is the outcome the caller
        wanted. Every other Wi-Fi failure raises
        :class:`VizioWifiError`.

        Does not confirm the device actually joined. It leaves its setup
        access point on success, so the calling host generally loses its
        route to it — re-discover the device on the target network
        instead.
        """
        secret = password or ""
        try:
            if hidden:
                await self._put_network_leaf(
                    Endpoint.HIDDEN_NETWORK,
                    lambda hashval: _payloads.join_hidden_network(
                        ssid=ssid, password=secret, hashval=hashval
                    ),
                )
                return

            await self._put_network_leaf(
                Endpoint.CURRENT_ACCESS_POINT,
                lambda hashval: _payloads.select_access_point(
                    ssid=ssid, hashval=hashval
                ),
            )
            await self._put_network_leaf(
                Endpoint.WIFI_PASSWORD,
                lambda hashval: _payloads.write_setting(
                    value=secret, hashval=hashval
                ),
            )
        except VizioWifiError as err:
            if err.result is WifiResult.ALREADY_CONNECTED:
                return
            raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/_device.py tests/test_wifi.py
git commit -m "feat: add join_access_point for Wi-Fi provisioning"
```

---

### Task 9: `WifiSetupSession`

**Files:**

- Modify: `src/vizaio/_device.py`
- Test: `tests/test_wifi.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_wifi.py`:

```python
from vizaio._device import WifiSetupSession


class _FakeVizio:
    """Records provisioning calls; lets tests inject failures."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.join_error: Exception | None = None
        self.aps: tuple[AccessPoint, ...] = ()

    async def start_ap_scan(self) -> None:
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error

    async def stop_ap_scan(self) -> None:
        self.calls.append("stop")
        if self.stop_error is not None:
            raise self.stop_error

    async def get_access_points(self) -> tuple[AccessPoint, ...]:
        self.calls.append("read")
        return self.aps

    async def join_access_point(
        self, ssid: str, *, password: str | None = None, hidden: bool = False
    ) -> None:
        self.calls.append(f"join:{ssid}:{password}:{hidden}")
        if self.join_error is not None:
            raise self.join_error


async def test_session_starts_and_stops_the_scan() -> None:
    fake = _FakeVizio()
    async with WifiSetupSession(fake):  # type: ignore[arg-type]
        pass
    assert fake.calls == ["start", "stop"]


async def test_session_stops_the_scan_when_the_body_raises() -> None:
    fake = _FakeVizio()
    with pytest.raises(RuntimeError, match="boom"):
        async with WifiSetupSession(fake):  # type: ignore[arg-type]
            raise RuntimeError("boom")
    assert fake.calls == ["start", "stop"]


async def test_session_stops_the_scan_when_join_raises() -> None:
    fake = _FakeVizio()
    fake.join_error = VizioWifiError(WifiResult.AUTH_REJECTED, "NET_WIFI_AUTH_REJECTED")
    with pytest.raises(VizioWifiError):
        async with WifiSetupSession(fake) as session:  # type: ignore[arg-type]
            await session.join("net", password="wrong")
    assert fake.calls[-1] == "stop"


async def test_failed_stop_does_not_mask_the_callers_exception() -> None:
    fake = _FakeVizio()
    fake.stop_error = VizioWifiError(WifiResult.UNKNOWN, "NET_UNKNOWN_ERROR")
    with pytest.raises(RuntimeError, match="original"):
        async with WifiSetupSession(fake):  # type: ignore[arg-type]
            raise RuntimeError("original")


async def test_failed_start_still_attempts_a_stop() -> None:
    fake = _FakeVizio()
    fake.start_error = VizioResponseError("nope")
    with pytest.raises(VizioResponseError):
        async with WifiSetupSession(fake):  # type: ignore[arg-type]
            pass
    assert fake.calls == ["start", "stop"]


async def test_access_points_is_re_readable() -> None:
    fake = _FakeVizio()
    async with WifiSetupSession(fake) as session:  # type: ignore[arg-type]
        await session.access_points()
        await session.access_points()
    assert fake.calls.count("read") == 2


async def test_join_is_re_callable_after_a_failure() -> None:
    fake = _FakeVizio()
    async with WifiSetupSession(fake) as session:  # type: ignore[arg-type]
        await session.join("net", password="first")
        await session.join("net", password="second")
    assert "join:net:first:False" in fake.calls
    assert "join:net:second:False" in fake.calls


async def test_wifi_setup_session_factory_returns_a_session() -> None:
    device = Vizio(host="h:9000", device_type=DeviceType.SOUNDBAR)
    assert isinstance(device.wifi_setup_session(), WifiSetupSession)
    await device.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: FAIL with `ImportError: cannot import name 'WifiSetupSession'`

- [ ] **Step 3: Write minimal implementation**

Add the factory method to `Vizio` in `src/vizaio/_device.py`:

```python
    def wifi_setup_session(self) -> WifiSetupSession:
        """
        Open a Wi-Fi provisioning session.

        Starts a scan on entry and always stops it on exit::

            async with vizio.wifi_setup_session() as session:
                for ap in await session.access_points():
                    print(ap.ssid)
                await session.join("MyNetwork", password="hunter2")

        The host must already be joined to the device's setup access
        point; that is an OS-level operation this library does not
        perform. The device's address is the DHCP gateway of that
        network.
        """
        return WifiSetupSession(self)
```

Add the class at module level, next to `PairSession`:

```python
class WifiSetupSession:
    """
    Async context manager that brackets a Wi-Fi provisioning flow.

    Starts an access-point scan on entry, always stops it on exit —
    including when the body raises, and including on the success path,
    which is what the official app does too. The stop is best-effort:
    a failure there is swallowed so it can never mask the caller's own
    exception.

    Unlike :class:`PairSession` there is no completion flag. Pairing
    must not cancel after a successful complete; stopping a scan after a
    successful join is correct, so the cleanup is unconditional.
    """

    def __init__(self, vizio: Vizio) -> None:
        """Bind the session to the device it will provision."""
        self._vizio = vizio

    async def __aenter__(self) -> Self:
        """Start the scan; best-effort stop if the start itself fails."""
        try:
            await self._vizio.start_ap_scan()
        except BaseException:
            await self._stop()
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Always stop the scan, swallowing cleanup failures."""
        await self._stop()

    async def _stop(self) -> None:
        """Stop the scan, ignoring device errors."""
        try:
            await self._vizio.stop_ap_scan()
        except VizioError:
            logger.debug("stop_ap_scan failed during session cleanup", exc_info=True)

    async def access_points(self) -> tuple[AccessPoint, ...]:
        """
        Read the current scan results.

        Performs one GET per call. Scans fill in over several seconds, so
        call this again to refresh rather than expecting the first read to
        be complete.
        """
        return await self._vizio.get_access_points()

    async def join(
        self,
        ssid: str,
        *,
        password: str | None = None,
        hidden: bool = False,
    ) -> None:
        """
        Hand the device credentials. Re-callable — retry a bad password
        without leaving the session.
        """
        await self._vizio.join_access_point(ssid, password=password, hidden=hidden)
```

Confirm `logger` exists in `_device.py`; if not, add `logger = logging.getLogger(__name__)` and
import `logging`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/_device.py tests/test_wifi.py
git commit -m "feat: add WifiSetupSession context manager"
```

---

### Task 10: Public exports

**Files:**

- Modify: `src/vizaio/__init__.py`
- Test: `tests/test_wifi.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_wifi.py`:

```python
def test_wifi_api_is_publicly_exported() -> None:
    import vizaio

    for name in ("AccessPoint", "WifiResult", "VizioWifiError", "WifiSetupSession"):
        assert name in vizaio.__all__
        assert getattr(vizaio, name) is not None


def test_all_is_sorted() -> None:
    import vizaio

    assert vizaio.__all__ == sorted(vizaio.__all__)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: FAIL with `AssertionError` — `AccessPoint` not in `__all__`

- [ ] **Step 3: Write minimal implementation**

In `src/vizaio/__init__.py`:

- change `from ._device import PairSession, Vizio` to
  `from ._device import PairSession, Vizio, WifiSetupSession`
- add `VizioWifiError` to the `from .errors import (...)` block
- add `AccessPoint` and `WifiResult` to the `from .types import (...)` block
- add `"AccessPoint"`, `"VizioWifiError"`, `"WifiResult"`, `"WifiSetupSession"` to `__all__`,
  keeping it sorted

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_wifi.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/__init__.py tests/test_wifi.py
git commit -m "feat: export the Wi-Fi provisioning API"
```

---

### Task 11: Captured hardware fixtures

**Files:**

- Create: `tests/captured/network_start_ap_search.json`
- Create: `tests/captured/network_wireless_access_points.json`
- Create: `tests/captured/network_current_access_point_unset.json`
- Create: `tests/captured/network_set_wifi_password.json`
- Create: `tests/captured/network_stop_ap_search.json`
- Create: `tests/captured/network_current_access_point_put.json`
- Modify: `tests/test_captured_replay.py`

- [ ] **Step 1: Write the fixtures**

These are the real payloads from issue #40 with SSIDs and BSSIDs redacted. Create each file
verbatim.

`tests/captured/network_start_ap_search.json`:

```json
{"STATUS":{"RESULT":"SUCCESS","DETAIL":"Success"},"ITEMS":[{"HASHVAL":300381621,"CNAME":"start_ap_search","TYPE":"T_ACTION_V1","NAME":"Start AP Search","VALUE":"T_ACTION_V1"}],"HASHLIST":[2488303405,246603788],"URI":"/menu_native/dynamic/audio_settings/network/start_ap_search","PARAMETERS":{"FLAT":"TRUE","HELPTEXT":"FALSE","HASHONLY":"FALSE"}}
```

`tests/captured/network_wireless_access_points.json`:

```json
{"STATUS":{"RESULT":"SUCCESS","DETAIL":"Success"},"ITEMS":[{"HASHVAL":203850784,"CNAME":"wireless_access_points","TYPE":"T_APS_V1","NAME":"Wireless Access Points","VALUE":[{"EM":"WPA2/PSK","RSSI":65,"NAME":"REDACTED-5G","BSSID":"00:00:00:00:00:01","BAND":"5"},{"EM":"WPA2/PSK","RSSI":70,"NAME":"REDACTED-24","BSSID":"00:00:00:00:00:02","BAND":"2.4"},{"EM":"WPA2/PSK","RSSI":45,"NAME":"REDACTED-NEIGHBOR","BSSID":"00:00:00:00:00:03","BAND":"2.4"}]}],"HASHLIST":[279391622,1354918630],"URI":"/menu_native/dynamic/audio_settings/network/wireless_access_points","PARAMETERS":{"FLAT":"TRUE","HELPTEXT":"FALSE","HASHONLY":"FALSE"}}
```

`tests/captured/network_current_access_point_unset.json`:

```json
{"STATUS":{"RESULT":"SUCCESS","DETAIL":"Success"},"ITEMS":[{"HASHVAL":3250072061,"CNAME":"current_access_point","TYPE":"T_AP_V1","NAME":"Current Access Point","VALUE":[{"EM":"NONE","RSSI":0,"NAME":"","BSSID":"000000-000000","BAND":"2.4"}]}],"HASHLIST":[2670078080,1346972001],"URI":"/menu_native/dynamic/audio_settings/network/current_access_point","PARAMETERS":{"FLAT":"TRUE","HELPTEXT":"FALSE","HASHONLY":"FALSE"}}
```

`tests/captured/network_current_access_point_put.json` — the response to a successful selection.
Note it has no `CNAME`:

```json
{"STATUS":{"RESULT":"SUCCESS","DETAIL":"Success"},"ITEMS":[{"HASHVAL":964400715,"NAME":"Current Access Point"}],"HASHLIST":[600369272,3181659690],"URI":"/menu_native/dynamic/audio_settings/network/current_access_point","PARAMETERS":{"HASHVAL":3250072061,"REQUEST":"MODIFY","VALUE":[{"NAME":"REDACTED-24"}]}}
```

`tests/captured/network_set_wifi_password.json`:

```json
{"STATUS":{"RESULT":"SUCCESS","DETAIL":"Success"},"ITEMS":[{"HASHVAL":2698362233,"CNAME":"set_wifi_password","TYPE":"T_STRING_V1","NAME":"Set Wi-Fi password","VALUE":""}],"HASHLIST":[3432854642,3688654204],"URI":"/menu_native/dynamic/audio_settings/network/set_wifi_password","PARAMETERS":{"FLAT":"TRUE","HELPTEXT":"FALSE","HASHONLY":"FALSE"}}
```

`tests/captured/network_stop_ap_search.json`:

```json
{"STATUS":{"RESULT":"SUCCESS","DETAIL":"Success"},"ITEMS":[{"HASHVAL":139197155,"CNAME":"stop_ap_search","TYPE":"T_ACTION_V1","NAME":"Stop AP Search","VALUE":"T_ACTION_V1"}],"HASHLIST":[2158910621,1896934952],"URI":"/menu_native/dynamic/audio_settings/network/stop_ap_search","PARAMETERS":{"FLAT":"TRUE","HELPTEXT":"FALSE","HASHONLY":"FALSE"}}
```

- [ ] **Step 2: Write the failing replay tests**

Append to `tests/test_captured_replay.py`:

```python
from vizaio.parse import parse_access_points, parse_current_access_point
from vizaio.types import SettingType


def test_captured_scan_list_parses() -> None:
    response = _load("network_wireless_access_points")
    aps = parse_access_points(response)
    assert len(aps) == 3
    assert {a.band for a in aps} == {"2.4", "5"}
    assert all(not a.is_open for a in aps)
    assert response.items[0].type == SettingType.ACCESS_POINTS


def test_captured_unset_access_point_reads_as_none() -> None:
    assert parse_current_access_point(_load("network_current_access_point_unset")) is None


def test_captured_action_leaves_expose_hashvals() -> None:
    for name, expected in (
        ("network_start_ap_search", 300381621),
        ("network_stop_ap_search", 139197155),
        ("network_set_wifi_password", 2698362233),
    ):
        assert _load(name).items[0].hashval == expected


def test_captured_write_response_has_no_cname() -> None:
    # This is why none of the network endpoint rows may set ``item=``:
    # a successful write returns an ITEM the client could never match.
    response = _load("network_current_access_point_put")
    assert response.status is ResponseStatus.SUCCESS
    assert response.items[0].cname == ""
    assert response.items[0].hashval == 964400715
```

Add a `_load` helper if the module does not already have one — check the existing file first and
reuse its loader rather than duplicating it. If it loads fixtures inline, add:

```python
def _load(name: str) -> Response:
    """Read a captured fixture and parse it into a Response."""
    path = Path(__file__).parent / "captured" / f"{name}.json"
    return Response.from_json(json.loads(path.read_text()))
```

Also update the module docstring: it currently says every fixture came from a VHD24M-0810. Add a
sentence noting the `network_*` fixtures came from a soundbar in SoftAP setup mode (issue #40),
making them the suite's first from a second device family and first from a device mid-onboarding.

- [ ] **Step 3: Run tests to verify they fail, then pass**

Run: `uv run pytest tests/test_captured_replay.py -v`
Expected: initially FAIL if the fixtures are missing; PASS once all six files exist.

- [ ] **Step 4: Commit**

```bash
git add tests/captured tests/test_captured_replay.py
git commit -m "test: add captured soundbar provisioning payloads"
```

---

### Task 12: CLI — `vizaio wifi scan` and `vizaio wifi join`

**Files:**

- Modify: `src/vizaio/cli/__init__.py`
- Test: `tests/test_cli_wifi.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_wifi.py`:

```python
"""CLI tests for ``vizaio wifi``."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from vizaio.cli import app
from vizaio.errors import VizioWifiError
from vizaio.types import AccessPoint, WifiResult

runner = CliRunner()

_APS = (
    AccessPoint(ssid="HomeNet", bssid="aa", security="WPA2/PSK", band="5", rssi=65),
    AccessPoint(ssid="OpenNet", bssid="bb", security="NONE", band="2.4", rssi=40),
)


def _patch_device(**attrs: object):
    """Patch Vizio in the CLI module with an AsyncMock carrying ``attrs``."""
    device = AsyncMock()
    device.__aenter__.return_value = device
    device.get_access_points.return_value = _APS
    for key, value in attrs.items():
        setattr(device, key, value)
    return patch("vizaio.cli.Vizio", return_value=device), device


def test_wifi_scan_lists_networks() -> None:
    patcher, device = _patch_device()
    resolver = AsyncMock(return_value="h:9000")
    with patcher, patch("vizaio.cli.async_resolve_host", resolver):
        result = runner.invoke(app, ["wifi", "scan", "1.2.3.4"])
    assert result.exit_code == 0
    # A bare IP must go through port probing (7345, then 9000).
    resolver.assert_awaited_once_with("1.2.3.4")
    assert "HomeNet" in result.stdout
    assert "OpenNet" in result.stdout
    device.start_ap_scan.assert_awaited_once()
    device.stop_ap_scan.assert_awaited_once()


def test_wifi_join_passes_credentials() -> None:
    patcher, device = _patch_device()
    with patcher, patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")):
        result = runner.invoke(
            app, ["wifi", "join", "1.2.3.4", "HomeNet", "--password", "pw"]
        )
    assert result.exit_code == 0
    device.join_access_point.assert_awaited_once_with(
        "HomeNet", password="pw", hidden=False
    )


def test_wifi_join_reports_a_rejected_password() -> None:
    patcher, device = _patch_device(
        join_access_point=AsyncMock(
            side_effect=VizioWifiError(WifiResult.AUTH_REJECTED, "NET_WIFI_AUTH_REJECTED")
        )
    )
    with patcher, patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")):
        result = runner.invoke(
            app, ["wifi", "join", "1.2.3.4", "HomeNet", "--password", "bad"]
        )
    assert result.exit_code == 1
    assert "NET_WIFI_AUTH_REJECTED" in result.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_wifi.py -v`
Expected: FAIL — `No such command 'wifi'`

- [ ] **Step 3: Write minimal implementation**

In `src/vizaio/cli/__init__.py`, add `async_resolve_host` to the `..discovery` import and
`AccessPoint` to the `..` import. Add this section before the `vizaio power` section:

```python
# ---------------------------------------------------------------------------
# `vizaio wifi ...` — soundbar Wi-Fi provisioning over its setup hotspot
# ---------------------------------------------------------------------------

wifi_app = typer.Typer(
    name="wifi",
    help=(
        "Provision a device's Wi-Fi over its setup access point. "
        "Join the device's hotspot first; its address is your gateway."
    ),
)
app.add_typer(wifi_app)

HostArgument = Annotated[
    str,
    typer.Argument(
        help=(
            "Device IP or IP:PORT. While joined to the setup hotspot this is "
            "your default gateway. A bare IP probes ports 7345 and 9000."
        )
    ),
]
WifiDeviceTypeOption = Annotated[
    DeviceType, typer.Option("--device-type", help="Device family.")
]


def _ap_rows(access_points: tuple[AccessPoint, ...]) -> list[dict[str, Any]]:
    """Render access points as printable rows."""
    return [
        {
            "ssid": ap.ssid,
            "band": f"{ap.band} GHz",
            "security": ap.security or "NONE",
            "signal": str(ap.rssi),
        }
        for ap in access_points
    ]


def _wifi_exec[T](
    host: str,
    device_type: DeviceType,
    fn: Callable[[Vizio], Awaitable[T]],
) -> T:
    """
    Run ``fn`` against a device addressed directly by host.

    Bypasses the usual alias resolution: a device in setup mode has no
    saved alias, exactly as with ``vizaio pair``.
    """

    async def _go() -> T:
        """Resolve the port, open a session, and run ``fn``."""
        resolved = await async_resolve_host(host)
        async with Vizio(host=resolved, device_type=device_type) as v:
            return await fn(v)

    try:
        return asyncio.run(_go())
    except VizioError as e:
        _err.print(f"vizaio: {e}")
        raise typer.Exit(code=1) from e


@wifi_app.command("scan")
def wifi_scan(
    ctx: typer.Context,
    host: HostArgument,
    device_type: WifiDeviceTypeOption = DeviceType.SOUNDBAR,
    output_format: FormatOption = None,
) -> None:
    """Scan for nearby Wi-Fi networks and print what the device can see."""
    fmt = _fmt(ctx, output_format)

    async def _go(v: Vizio) -> tuple[AccessPoint, ...]:
        """Run one scan cycle and return the results."""
        async with v.wifi_setup_session() as session:
            return await session.access_points()

    access_points = _wifi_exec(host, device_type, _go)
    if not access_points:
        _err.print("No networks found. The scan may need another moment — retry.")
        return
    _print(render_rows(_ap_rows(access_points), fmt=fmt))


@wifi_app.command("join")
def wifi_join(
    ctx: typer.Context,
    host: HostArgument,
    ssid: Annotated[str, typer.Argument(help="Network name to join.")],
    password: Annotated[
        str | None, typer.Option("--password", help="Network password.")
    ] = None,
    hidden: Annotated[
        bool, typer.Option("--hidden", help="SSID is not broadcast.")
    ] = False,
    device_type: WifiDeviceTypeOption = DeviceType.SOUNDBAR,
    output_format: FormatOption = None,
) -> None:
    """Hand Wi-Fi credentials to the device."""
    fmt = _fmt(ctx, output_format)

    async def _go(v: Vizio) -> None:
        """Join within a session so the scan is always stopped."""
        async with v.wifi_setup_session() as session:
            await session.join(ssid, password=password, hidden=hidden)

    _wifi_exec(host, device_type, _go)
    _print(
        render_message(
            f"Credentials sent for {ssid!r}. Rejoin your normal Wi-Fi, then run "
            "`vizaio discover` to find the device on your network.",
            fmt=fmt,
        )
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_wifi.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/cli/__init__.py tests/test_cli_wifi.py
git commit -m "feat: add vizaio wifi scan and join commands"
```

---

### Task 13: CLI — `vizaio wifi interactive`

**Files:**

- Modify: `src/vizaio/cli/__init__.py`
- Test: `tests/test_cli_wifi.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_wifi.py`:

```python
def test_interactive_selects_a_network_and_prompts_for_a_password() -> None:
    patcher, device = _patch_device()
    with patcher, patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")):
        # "1" picks HomeNet (secured), then the password.
        result = runner.invoke(app, ["wifi", "interactive", "1.2.3.4"], input="1\npw\n")
    assert result.exit_code == 0
    device.join_access_point.assert_awaited_once_with(
        "HomeNet", password="pw", hidden=False
    )
    assert "vizaio discover" in result.stdout


def test_interactive_skips_the_password_prompt_for_an_open_network() -> None:
    patcher, device = _patch_device()
    with patcher, patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")):
        # "2" picks OpenNet; no password line follows.
        result = runner.invoke(app, ["wifi", "interactive", "1.2.3.4"], input="2\n")
    assert result.exit_code == 0
    device.join_access_point.assert_awaited_once_with(
        "OpenNet", password=None, hidden=False
    )


def test_interactive_handles_a_hidden_network() -> None:
    patcher, device = _patch_device()
    with patcher, patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")):
        result = runner.invoke(
            app, ["wifi", "interactive", "1.2.3.4"], input="0\nghost\npw\n"
        )
    assert result.exit_code == 0
    device.join_access_point.assert_awaited_once_with(
        "ghost", password="pw", hidden=True
    )


def test_interactive_reprompts_after_a_rejected_password() -> None:
    device = AsyncMock()
    device.__aenter__.return_value = device
    device.get_access_points.return_value = _APS
    device.join_access_point = AsyncMock(
        side_effect=[
            VizioWifiError(WifiResult.AUTH_REJECTED, "NET_WIFI_AUTH_REJECTED"),
            None,
        ]
    )
    with (
        patch("vizaio.cli.Vizio", return_value=device),
        patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")),
    ):
        result = runner.invoke(
            app, ["wifi", "interactive", "1.2.3.4"], input="1\nwrong\nright\n"
        )
    assert result.exit_code == 0
    assert device.join_access_point.await_count == 2


def test_interactive_rescans_when_nothing_is_found() -> None:
    device = AsyncMock()
    device.__aenter__.return_value = device
    device.get_access_points = AsyncMock(side_effect=[(), _APS])
    with (
        patch("vizaio.cli.Vizio", return_value=device),
        patch("vizaio.cli.async_resolve_host", AsyncMock(return_value="h:9000")),
    ):
        # "y" retries the scan, then "1" picks HomeNet, then the password.
        result = runner.invoke(
            app, ["wifi", "interactive", "1.2.3.4"], input="y\n1\npw\n"
        )
    assert result.exit_code == 0
    assert device.get_access_points.await_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_wifi.py -v`
Expected: FAIL — `No such command 'interactive'`

- [ ] **Step 3: Write minimal implementation**

Append to the `vizaio wifi` section in `src/vizaio/cli/__init__.py`:

```python
_HIDDEN_CHOICE = 0


async def _prompt_for_network(
    session: WifiSetupSession,
) -> tuple[str, str | None, bool]:
    """
    Scan, show the results, and ask which network to join.

    Returns ``(ssid, password, hidden)``. Re-reads the scan list on
    request rather than polling on a timer — scans fill in over several
    seconds and only the user knows how long is long enough.
    """
    while True:
        access_points = await session.access_points()
        if access_points:
            break
        if not typer.confirm("No networks found yet — scan again?", default=True):
            raise typer.Exit(code=1)

    _err.print("")
    for index, ap in enumerate(access_points, start=1):
        lock = "open" if ap.is_open else ap.security
        _err.print(f"  {index}) {ap.ssid}  [{ap.band} GHz, {lock}, signal {ap.rssi}]")
    _err.print(f"  {_HIDDEN_CHOICE}) Hidden network…\n")

    choice = typer.prompt("Select network", type=int)
    while choice < _HIDDEN_CHOICE or choice > len(access_points):
        choice = typer.prompt("Select network", type=int)

    if choice == _HIDDEN_CHOICE:
        ssid = typer.prompt("Network name")
        return ssid, typer.prompt("Password", hide_input=True), True

    chosen = access_points[choice - 1]
    if chosen.is_open:
        return chosen.ssid, None, False
    return chosen.ssid, typer.prompt("Password", hide_input=True), False


@wifi_app.command("interactive")
def wifi_interactive(
    ctx: typer.Context,
    host: HostArgument,
    device_type: WifiDeviceTypeOption = DeviceType.SOUNDBAR,
    output_format: FormatOption = None,
) -> None:
    """Scan, pick a network, and hand over credentials, step by step."""
    fmt = _fmt(ctx, output_format)

    async def _go(v: Vizio) -> str:
        """Drive the full wizard inside one provisioning session."""
        async with v.wifi_setup_session() as session:
            ssid, password, hidden = await _prompt_for_network(session)
            while True:
                try:
                    await session.join(ssid, password=password, hidden=hidden)
                    return ssid
                except VizioWifiError as err:
                    if err.result not in (
                        WifiResult.AUTH_REJECTED,
                        WifiResult.MISSING_PASSWORD,
                    ):
                        raise
                    _err.print(f"Device rejected the password ({err.code}).")
                    password = typer.prompt("Password", hide_input=True)

    joined = _wifi_exec(host, device_type, _go)
    _print(
        render_message(
            f"Credentials sent for {joined!r}. Rejoin your normal Wi-Fi, then run "
            "`vizaio discover` to find the device on your network.",
            fmt=fmt,
        )
    )
```

Add `WifiSetupSession`, `VizioWifiError` and `WifiResult` to the `from .. import (...)` block at the
top of the file.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_wifi.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vizaio/cli/__init__.py tests/test_cli_wifi.py
git commit -m "feat: add the vizaio wifi interactive wizard"
```

---

### Task 14: Documentation

**Files:**

- Modify: `docs/protocol-notes.md` (§32)
- Modify: `README.md`

- [ ] **Step 1: Update the protocol notes confidence line**

In `docs/protocol-notes.md` §32, change:

```
**Confidence:** APK DERIVED — NOT HARDWARE VERIFIED
```

to:

```
**Confidence:** HARDWARE VERIFIED (visible-network path); APK DERIVED for the
hidden-network path
```

- [ ] **Step 2: Record the hardware findings**

Immediately after the status-vocabulary table in §32, insert:

```markdown
**Hardware verification (issue #40, soundbar in SoftAP setup mode).** The full
visible-network sequence was executed successfully against a real device. Three
findings could not have been derived from the APK:

- **The device answers on port 9000**, not 7345. `discovery.DEFAULT_PORTS`
  already probes both.
- **No `AUTH` header is required.** Every request succeeded unauthenticated,
  consistent with the soundbar profile's `requires_auth=False`.
- **`current_access_point` accepts `NAME` alone.** The `NAME` + `PASSWORD`
  variant that `AccessPointsViewModel` sends was tried and did *not* work on
  this firmware; `NAME` alone did. `select_access_point` sends only `NAME`.

Two shapes worth noting for implementers. The scan list reports
`EM`/`RSSI`/`NAME`/`BSSID`/`BAND` but **not** the `OPEN` or `CONNECTED` fields
`VZAccessPointItem` declares. And a successful write returns
`ITEMS: [{"HASHVAL": …, "NAME": "Current Access Point"}]` with **no `CNAME`** —
which is why none of the network endpoint rows may declare an `item` cname.

Still unverified: the hidden-network path, and every `NET_*` failure code (the
run returned `SUCCESS` at every step).
```

- [ ] **Step 3: Replace the "Our handling" paragraph**

In §32, replace the paragraph beginning "**Our handling:** not implemented as a
dedicated API." with:

```markdown
**Our handling:** `Vizio.start_ap_scan`, `stop_ap_scan`, `get_access_points`,
`get_current_access_point` and `join_access_point` expose the primitives;
`Vizio.wifi_setup_session()` brackets them so the scan is always stopped. The
CLI offers `vizaio wifi scan` / `join` / `interactive`. `NET_*` results raise
`VizioWifiError` carrying a parsed `WifiResult`, except
`NET_WIFI_ALREADY_CONNECTED`, which `join_access_point` treats as success
because the device is already where the caller wanted it.

We deliberately diverge from the app on `NET_WIFI_NEEDS_VALID_SSID`: its success
predicate is `isSuccessful() || isWifiNeedsValidSsid()`, which looks like a
workaround for its own UI ordering. Swallowing it would report a failed
provision as success, so we raise.
```

- [ ] **Step 4: Add the README example**

In `README.md`, after the pairing example, add:

````markdown
### Provisioning a soundbar's Wi-Fi

A factory-fresh soundbar broadcasts an open setup hotspot. Join it first — that
is an OS-level step this library does not perform — then point `vizaio` at your
default gateway, which is the device's address on its own network.

```python
from vizaio import DeviceType, Vizio

async with Vizio(host="192.168.1.1:9000", device_type=DeviceType.SOUNDBAR) as v:
    async with v.wifi_setup_session() as session:
        for ap in await session.access_points():
            print(ap.ssid, ap.band, "open" if ap.is_open else ap.security)
        await session.join("MyNetwork", password="hunter2")
```

Or interactively:

```console
$ vizaio wifi interactive 192.168.1.1
```

Neither confirms the device joined — it leaves its hotspot on success, so the
host loses its route. Rejoin your normal network and run `vizaio discover`.
````

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest`
Expected: full suite passes.

```bash
git add docs/protocol-notes.md README.md
git commit -m "docs: document Wi-Fi provisioning as hardware verified"
```

---

## Final verification

- [ ] Run the full suite: `uv run pytest`
- [ ] Run the linters the pre-commit hooks enforce: `uv run ruff check . && uv run ruff format
      --check . && uv run mypy src`
- [ ] Confirm `vizaio wifi --help`, `vizaio wifi scan --help`, `vizaio wifi join --help` and `vizaio
      wifi interactive --help` all render
