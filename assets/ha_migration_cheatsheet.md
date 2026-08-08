# Migrating from `pyvizio` to `vizaio`

A method-by-method mapping for the Home Assistant `vizio` integration and
other downstream consumers. Behavior is summarized; full details live in
the test suite (`tests/test_device.py`).

## Top-level changes

| Change | Old (`pyvizio`) | New (`vizaio`) |
|--------|-----------------|--------------------------|
| Sync support | `Vizio` (sync) + `VizioAsync` | `Vizio` only — async-only |
| Constructor | `(device_id, ip, name, auth, device_type)` positional | `(host, *, device_type=, auth_token=)` keyword |
| Failure mode | Returns `None` on error, with optional `log_api_exception` | Raises typed exception subclasses of `VizioError` |
| Action returns | `True` / `None` | `None` (raises on failure) |
| Getter returns | `T \| None` | `T` (raises on failure) |
| Lifecycle | None — sessions per-request | Async context manager owns session |
| Pairing | Three separate methods | `pair_session()` async context manager |

## Constructor

```python
# OLD
v = VizioAsync("ha-coord", "192.168.1.50:7345", "HomeAssistant", "TOKEN", "tv")

# NEW
async with Vizio(
    host="192.168.1.50:7345", device_type=DeviceType.TV, auth_token="TOKEN"
) as v:
    ...
```

`device_id` and `device_name` move to `pair_session(device_id, device_name)`.
They were only used during pairing and are now passed at the right time.

## Power

| Old | New | Notes |
|-----|-----|-------|
| `await v.get_power_state()` returns `bool \| None` | `await v.get_power_state()` returns `bool`, raises | |
| `await v.pow_on()` returns `True`/`None` | `await v.power_on()` returns `None`, raises | |
| `await v.pow_off()` returns `True`/`None` | `await v.power_off()` returns `None`, raises | |
| `await v.pow_toggle()` | *removed* | Compose: `(power_off if await v.get_power_state() else power_on)()` |

## Volume / mute

| Old | New | Notes |
|-----|-----|-------|
| `await v.get_current_volume()` | `await v.get_volume()` returns `int`, raises | |
| `await v.vol_up(num=N)` | `await v.volume_up(steps=N)` | Param renamed from `num` to `steps`; chunks at 20/PUT for large N |
| `await v.vol_down(num=N)` | `await v.volume_down(steps=N)` | |
| `await v.is_muted()` | `await v.is_muted()` returns `bool`, raises | |
| `await v.mute_on()` | `await v.mute()` | |
| `await v.mute_off()` | `await v.unmute()` | |
| `await v.mute_toggle()` | *removed* | Compose: `(unmute if await v.is_muted() else mute)()` |
| `v.get_max_volume()` | `v.profile.max_volume` | Property on the device profile |

## Channel / media

| Old | New | Notes |
|-----|-----|-------|
| `await v.ch_up()` | `await v.send_key(RemoteKey.CH_UP)` | |
| `await v.ch_down()` | `await v.send_key(RemoteKey.CH_DOWN)` | |
| `await v.ch_prev()` | `await v.send_key(RemoteKey.CH_PREV)` | |
| `await v.play()` | `await v.send_key(RemoteKey.PLAY)` | |
| `await v.pause()` | `await v.send_key(RemoteKey.PAUSE)` | |
| `await v.remote(key_str)` returns `bool \| None`,`False` for invalid | `await v.send_key(key)` raises `VizioUnsupportedError` for invalid | Loud failures replace silent ones |
| `v.get_remote_keys_list()` | `v.available_keys` | Property; returns `frozenset[str]` |

## Inputs

| Old | New | Notes |
|-----|-----|-------|
| `await v.get_inputs_list()` returns `list[InputItem] \| None` | `await v.get_inputs()` returns `list[InputInfo]`, raises | `InputInfo` is a frozen dataclass; `is_current: bool` is on the matching input |
| `await v.get_current_input()` | unchanged signature, raises on error | |
| `await v.set_input(name)` | unchanged, raises | Validates name against current inputs, raises `VizioInvalidInputError` with valid names listed |
| `await v.next_input()` | unchanged, raises | |

## Settings — major reshape

| Old | New | Notes |
|-----|-----|-------|
| `await v.get_setting_types_list()` | `await v.get_setting_types()` | |
| `await v.get_all_settings(type)` returns `dict[str, value]` | `await v.get_settings(type)` returns `dict[str, SettingInfo]` | `SettingInfo` carries value + hashval + metadata in one type |
| `await v.get_all_settings_options(type)` | folded into `get_settings()` | `SettingInfo.options` / `min` / `max` / `center` |
| `await v.get_all_settings_options_xlist(type)` | folded into `get_settings()` | XList items present `SettingInfo.type == SettingType.LIST_X` |
| `await v.get_setting(type, name)` returns raw value | `await v.get_setting(type, name)` returns `SettingInfo` | Caller reads `.value`, `.hashval`, etc. |
| `await v.get_setting_options(type, name)` | `(await v.get_setting(type, name)).options` | |
| `await v.get_setting_options_xlist(type, name)` | folded — same path | |
| `await v.set_setting(type, name, value)` does GET-then-PUT | `await v.set_setting(type, name, value, *, hashval=None)` | Caller-supplied hashval skips the GET (1 round trip) |
| Audio convenience methods (`get_audio_setting`, etc.) | *removed* | Use `get_setting("audio", name)` directly |

### Hashval race recovery

When the device returns `invalid_parameter` from a setting PUT (stale
hashval), `set_setting()` automatically re-fetches the current hashval and
retries once. Caller sees no exception. If the retry also fails, the
second `VizioInvalidParameterError` propagates.

This auto-retry only fires when `hashval=` was NOT explicitly passed —
when caller-supplied, we trust the caller and propagate immediately.

## Apps

| Old | New | Notes |
|-----|-----|-------|
| `await v.get_current_app(apps_list=)` | `await v.get_current_app()` | No `apps_list` param — uses bundled+remote-fetched catalog internally; returns `None` instead of `NO_APP_RUNNING` sentinel |
| `await v.get_current_app_config()` | unchanged | Returns `AppConfig` (frozen dataclass) |
| `await v.launch_app(name, apps_list=)` | `await v.launch_app(name)` | |
| `await v.launch_app_config(APP_ID, NAME_SPACE, MESSAGE)` | `await v.launch_app_config(AppConfig(...))` | Single `AppConfig` arg |
| `VizioAsync.get_apps_list(country)` static | `await fetch_app_catalog()` module function | Returns full `AppRecord` list, not just names |

## Device info

| Old | New | Notes |
|-----|-----|-------|
| `await v.get_esn()` | unchanged signature, raises | |
| `await v.get_serial_number()` | unchanged, raises | |
| `await v.get_version()` | unchanged, raises | |
| `await v.get_model_name()` | unchanged, raises | |
| `VizioAsync.get_unique_id(ip, type)` static | `await Vizio(host, device_type=t).get_serial_number()` | Static method dropped; construct briefly |
| — | `await v.get_device_info()` | NEW: aggregate `DeviceInfo` (model + serial + esn + version + inputs) in one call |

## Battery (Crave 360)

| Old | New | Notes |
|-----|-----|-------|
| `await v.get_battery_level()` | unchanged, raises | Raises `VizioUnsupportedError` on TVs/soundbars before any HTTP |
| `await v.get_charging_status()` returns `int` | unchanged, returns `ChargingStatus` enum | |

## Pairing — context manager

```python
# OLD
challenge = await v.start_pair()
try:
    pin = ask_user_for_pin()
    auth = await v.pair(challenge.ch_type, challenge.token, pin)
except Exception:
    await v.stop_pair()  # easy to forget — leaves TV stuck

# NEW
async with v.pair_session(device_id="ha-coord", device_name="HomeAssistant") as session:
    print(f"Challenge type: {session.challenge.challenge_type}")
    pin = ask_user_for_pin()
    auth = await session.complete(pin=pin)
# Auto-cancels if complete() didn't fire — guaranteed cleanup.
```

The context manager guarantees `cancel_pair` on:

- Exceptions inside the `with` block
- `complete()` raising (wrong PIN, etc.)
- `complete()` never being called (user Ctrl-C)

A successful `complete()` does NOT cancel — the device is paired.

## Connection probes

| Old | New | Notes |
|-----|-----|-------|
| `await v.can_connect_no_auth_check()` returns `bool` | `await v.ping()` raises on failure | |
| `await v.can_connect_with_auth_check()` returns `bool` | `await v.ping_auth()` raises on failure | |
| `VizioAsync.validate_ha_config(ip, auth, type)` static | construct + `ping_auth()` | |

```python
# OLD
if not await v.can_connect_with_auth_check():
    raise ConfigEntryAuthFailed

# NEW
try:
    await v.ping_auth()
except VizioAuthError as e:
    raise ConfigEntryAuthFailed from e
except VizioConnectionError as e:
    raise ConfigEntryNotReady from e
```

## Discovery

| Old | New | Notes |
|-----|-----|-------|
| `VizioAsync.discovery_zeroconf(timeout)` static, sync | `await discover_zeroconf(timeout=)` | Module-level, async. Requires `vizaio[discovery]` extra |
| `VizioAsync.discovery_ssdp(timeout)` static, sync | `await discover_ssdp(timeout=)` | Module-level, async. No external deps |
| — | `await discover(timeout=)` | NEW: runs both protocols concurrently, dedupes by IP |

## `log_api_exception`

Removed everywhere. Old behavior:

- `True` (default): log + return None
- `False`: silently return None

New behavior: methods raise. Callers control logging:

```python
# OLD
volume = await v.get_current_volume(log_api_exception=False)
if volume is None:
    self.async_set_unavailable()

# NEW
try:
    volume = await v.get_volume()
except VizioError as e:
    _LOGGER.debug("Failed to get volume: %s", e)
    self.async_set_unavailable()
```
