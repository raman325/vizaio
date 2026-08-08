"""
The public Vizio class.

Layered on top of:

- :class:`SmartCastClient` for transport
- :func:`resolve` + :class:`EndpointSpec` for endpoint dispatch
- :class:`Response` for typed wire data
- :mod:`_payloads` for PUT body construction
- :class:`DeviceProfile` for capability gating

Reliability features:

- Owned aiohttp session by default; cleaned up on aclose.
- Instance-level ``asyncio.Semaphore`` (via the underlying client) for
  write serialization.
- Hashval race recovery in ``set_setting``: on
  :class:`VizioInvalidParameterError` from PUT, refetch GET once and
  retry PUT before raising.
- ``set_input`` validates against the current input list, raises
  :class:`VizioInvalidInputError` with the valid names listed.
- Battery / app methods raise :class:`VizioUnsupportedError` (via the
  endpoint resolver) when the profile doesn't support them, before any
  request is sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import contextlib
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Self

from . import _payloads
from .apps import (
    APP_HOME,
    BUNDLED_APPS,
    BUNDLED_AVAILABILITY,
    NO_APP_RUNNING,
    UNKNOWN_APP,
    app_in_country,
    extract_chipset,
    fetch_app_availability,
    fetch_app_catalog,
    find_app_name,
    find_app_record,
    find_availability,
    pick_chipset_payload,
)
from .client import SmartCastClient
from .endpoints import (
    Endpoint,
    resolve,
)
from .errors import (
    VizioAuthError,
    VizioError,
    VizioInvalidInputError,
    VizioInvalidParameterError,
    VizioNotFoundError,
    VizioResponseError,
    VizioUnsupportedError,
)
from .parse import (
    parse_auth_token,
    parse_current_app_config,
    parse_current_input,
    parse_current_input_item,
    parse_firmware_version,
    parse_inputs,
    parse_model_name,
    parse_pair_challenge,
    parse_setting,
    parse_setting_types,
    parse_settings,
    parse_state_extended,
    parse_system_info,
    parse_system_versions,
    parse_vizios_binary,
)
from .types import (
    AppAvailability,
    AppConfig,
    AppRecord,
    AuthRequirement,
    ChargingStatus,
    DeviceInfo,
    DeviceProfile,
    DeviceType,
    InputInfo,
    PairChallenge,
    SettingInfo,
    SettingType,
    StateExtended,
    SystemVersions,
)
from .wire import Response

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aiohttp import ClientSession, ClientTimeout

# Per APK findings (protocol-notes #19): the official app sends
# arbitrary-length KEYLISTs in one PUT. We chunk above this defensive
# cap purely to handle worst-case device buffers.
_KEYLIST_CHUNK_SIZE: Final = 50

# App catalog refresh policy.
_APP_CATALOG_TTL: Final = timedelta(hours=24)

# Availability data rolls forward with firmware updates — TTL is shorter so
# users on a fresh firmware see new apps without restarting their client.
_APP_AVAILABILITY_TTL: Final = timedelta(hours=6)

# Per-field identity endpoints used as fallback when the aggregate
# tv_information endpoint isn't exposed (older firmware). Modern firmware
# rejects these per-field paths with URI_NOT_FOUND; the aggregate path
# returns all fields in one shot.
_PER_FIELD_IDENTITY_ENDPOINTS: Final[dict[str, Endpoint]] = {
    "esn": Endpoint.ESN,
    "serial_number": Endpoint.SERIAL_NUMBER,
    "version": Endpoint.VERSION,
}

# Identity fields the unauthenticated deviceinfo payload carries under
# SYSTEM_INFO, keyed by the cname the identity getters use. These are read
# from deviceinfo first (no auth token needed, no menu_native round trip)
# before falling back to the auth-gated aggregate / per-field endpoints.
# ESN is deliberately absent — deviceinfo's SYSTEM_INFO has no ESN field
# (verified live on VHD24M-0810 fw 3.720.9.1-1), so get_esn() always uses
# the menu_native path.
_DEVICEINFO_IDENTITY_FIELDS: Final[frozenset[str]] = frozenset(
    {"serial_number", "version"}
)

# Errors that mean "this device or firmware doesn't offer the field on this
# path", as opposed to "the device is broken or unreachable". Only these may
# fall through to the next identity source; anything else must surface,
# otherwise "" conflates a TV with no serial number and a TV that is down.
_IDENTITY_FALLTHROUGH: Final = (
    VizioNotFoundError,
    VizioUnsupportedError,
    VizioAuthError,
)


class Vizio:
    """Async client for a single Vizio SmartCast device."""

    def __init__(
        self,
        host: str,
        *,
        device_type: DeviceType | None = None,
        profile: DeviceProfile | None = None,
        auth_token: str | None = None,
        session: ClientSession | None = None,
        timeout: float | ClientTimeout | None = None,
        max_concurrent_requests: int = 1,
        apps: tuple[AppRecord, ...] | None = None,
        availability: tuple[AppAvailability, ...] | None = None,
    ) -> None:
        """
        Configure the client.

        Pass at most one of ``device_type`` or ``profile``. When both are
        omitted, defaults to ``DeviceType.TV.profile``.
        """
        if profile is not None and device_type is not None:
            raise ValueError("specify at most one of device_type or profile")
        if profile is None:
            profile = (device_type or DeviceType.TV).profile

        self._host = host
        self._profile = profile
        self._auth_token = auth_token

        self._client = SmartCastClient(
            host=host,
            auth_token=auth_token,
            session=session,
            timeout=timeout,
            max_concurrent=max_concurrent_requests,
        )

        # Optional injection points for callers (e.g., Home Assistant
        # coordinators) that want to fetch + cache the catalog and
        # availability data themselves and share them across many
        # Vizio instances. When supplied, the instance treats them as
        # fresh forever and never auto-fetches. Set ``None`` (default)
        # to use the lib's own per-instance, in-memory, TTL-bounded
        # cache. The lib never writes either to disk.
        now = datetime.now() if apps or availability else None
        self._cached_apps: tuple[AppRecord, ...] | None = apps
        self._cached_apps_at: datetime | None = now if apps else None
        self._caller_owned_apps = apps is not None
        self._cached_availability: tuple[AppAvailability, ...] | None = availability
        self._cached_availability_at: datetime | None = now if availability else None
        self._caller_owned_availability = availability is not None
        # Identity is immutable for the device lifetime, so we cache it
        # for the full Vizio session. ``_loaded`` distinguishes "not
        # fetched yet" (None means try) from "fetched and aggregate
        # unavailable" (None means don't bother).
        self._cached_identity: dict[str, str] | None = None
        self._cached_identity_loaded = False
        # Chipset + firmware are read once per session from deviceinfo.
        # Empty string is the "no data" sentinel here — the device may
        # legitimately not expose either field on older firmware.
        self._cached_chipset: str | None = None
        self._cached_firmware: str | None = None
        # The raw deviceinfo response, cached for the session and shared by
        # model-name, chipset/firmware, and serial/version identity reads so
        # one unauthenticated ``state/device/deviceinfo`` GET serves them all.
        # The outcome is cached either way: a failure is remembered in
        # ``_cached_deviceinfo_error`` and re-raised rather than re-fetched
        # (deviceinfo is immutable — one attempt per session). The lock
        # coalesces concurrent callers so they issue a single GET.
        self._cached_deviceinfo: Response | None = None
        self._cached_deviceinfo_error: VizioError | None = None
        self._deviceinfo_lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        """Enter the async context manager (no-op; HTTP session is lazy)."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Close the underlying HTTP client on exit."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    @property
    def host(self) -> str:
        """The host string (``IP`` or ``IP:PORT``) this client targets."""
        return self._host

    @property
    def profile(self) -> DeviceProfile:
        """The :class:`DeviceProfile` driving capability + keymap behavior."""
        return self._profile

    @property
    def available_keys(self) -> frozenset[str]:
        """Names of remote keys this device supports."""
        return frozenset(self._profile.keymap.keys())

    # ------------------------------------------------------------------
    # Health probes
    # ------------------------------------------------------------------

    async def ping(self) -> None:
        """Cheap unauthenticated health check. Raises on failure."""
        await self._request(Endpoint.DEVICE_INFO)

    async def ping_auth(self) -> None:
        """
        Validate that the configured auth token is accepted.

        For TVs (``require_auth=True``) this exercises the auth token
        against an authenticated endpoint. For audio devices that don't
        require auth, this is equivalent to :meth:`ping`.
        """
        # Use the settings root — small payload, requires auth on TVs.
        await self._request(Endpoint.SETTINGS)

    # ------------------------------------------------------------------
    # Power
    # ------------------------------------------------------------------

    async def get_power_state(self) -> bool:
        """Return ``True`` if the device's screen/output is on."""
        response = await self._request(Endpoint.POWER_MODE)
        item = response.require_item("power_mode")
        return bool(item.value)

    async def power_on(self) -> None:
        """Send the power-on key."""
        await self.send_key("POW_ON")

    async def power_off(self) -> None:
        """Send the power-off key."""
        await self.send_key("POW_OFF")

    async def power_toggle(self) -> None:
        """
        Send the power-toggle key. Flips whatever state the device is in.

        Distinct from :meth:`power_on` / :meth:`power_off`, which always
        send the same key regardless of state. Use ``power_toggle`` when
        you want "press the power button" semantics — e.g., a single
        button in a UI — and don't need to guarantee the resulting state.
        """
        await self.send_key("POW_TOGGLE")

    # ------------------------------------------------------------------
    # Volume / mute
    # ------------------------------------------------------------------

    async def get_volume(self) -> int:
        """Return the device's current raw volume (range depends on profile)."""
        info = await self.get_setting("audio", "volume")
        return int(info.value)

    async def set_volume(self, level: int) -> None:
        """
        Set the absolute volume level.

        Uses the flat ``PUT /audio/volume/level`` endpoint with body
        ``{"LEVEL": n}`` — a single round trip with **no HASHVAL** dance
        (verified live on VHD24M-0810), unlike the ``menu_native`` audio
        path that ``set_setting`` would take. ``level`` is validated
        against the profile's ``max_volume``; out-of-range raises
        :class:`VizioInvalidInputError`.
        """
        max_volume = self._profile.max_volume
        if not 0 <= level <= max_volume:
            raise VizioInvalidInputError(f"volume {level} out of range 0-{max_volume}")
        await self._client.request_spec(
            resolve(Endpoint.VOLUME_LEVEL, self._profile),
            body=_payloads.volume_level(level),
        )

    async def volume_up(self, steps: int = 1) -> None:
        """Send ``steps`` volume-up keypresses in one PUT (default 1)."""
        await self._send_repeated_key("VOL_UP", steps)

    async def volume_down(self, steps: int = 1) -> None:
        """Send ``steps`` volume-down keypresses in one PUT (default 1)."""
        await self._send_repeated_key("VOL_DOWN", steps)

    async def is_muted(self) -> bool:
        """Return ``True`` if the device's mute setting is on."""
        info = await self.get_setting("audio", "mute")
        return str(info.value).lower() == "on"

    async def mute(self) -> None:
        """
        Mute the device. Idempotent — no-op if already muted.

        Implementation reads :meth:`is_muted` and sends ``MUTE_TOGGLE``
        only on mismatch. The discrete ``MUTE_ON``/``MUTE_OFF`` codes
        are firmware-class-specific (verified on VHD24M-0810 fw
        3.720.9.1-1: codeset 5 codes 2/3/4 all behave as toggles, codes
        5/6 return FAILURE). Soundbars and Crave speakers may have
        true discrete codes; ``MUTE_TOGGLE`` is the only universally
        reliable behavior. Power users wanting raw codes can call
        ``send_key("MUTE_ON")`` directly.
        """
        if not await self.is_muted():
            await self.send_key("MUTE_TOGGLE")

    async def unmute(self) -> None:
        """
        Unmute the device. Idempotent — no-op if already unmuted.

        See :meth:`mute` for the rationale on the read-then-toggle
        pattern.
        """
        if await self.is_muted():
            await self.send_key("MUTE_TOGGLE")

    async def mute_toggle(self) -> None:
        """
        Send the mute-toggle key. Flips whatever state the device is in.

        Cheaper than :meth:`mute` / :meth:`unmute` (one round trip vs.
        two — those methods read :meth:`is_muted` first to be
        idempotent). Use ``mute_toggle`` when you don't need to
        guarantee the resulting state and just want "press the mute
        button" semantics.
        """
        await self.send_key("MUTE_TOGGLE")

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    async def get_inputs(self) -> list[InputInfo]:
        """List all inputs on the device with ``is_current`` populated."""
        if not self._profile.has_inputs:
            raise VizioUnsupportedError(
                f"{self._profile.name} does not expose individual inputs"
            )
        # Modern firmware (verified VHD24M-0810 fw 3.720.9.1-1) does NOT
        # include a synthetic ``current_input`` entry inside the inputs
        # list response, so ``parse_inputs`` cannot deduce which one is
        # current on its own. Fetch ``current_input`` separately and pass
        # the value down so ``is_current`` is populated correctly across
        # firmware revisions. Older firmware that does include the
        # synthetic entry continues to work — ``parse_inputs`` will use
        # the inline value if present, our hint otherwise.
        inputs_response = await self._request(Endpoint.INPUTS)
        try:
            current_response = await self._request(Endpoint.CURRENT_INPUT)
            current_meta_name: str | None = parse_current_input(current_response)
        except VizioError:
            current_meta_name = None
        return parse_inputs(inputs_response, current_input_name=current_meta_name)

    async def get_current_input(self) -> str:
        """Return the meta_name of the active input (e.g., ``"SMARTCAST"``)."""
        response = await self._request(Endpoint.CURRENT_INPUT)
        return parse_current_input(response)

    async def set_input(self, name: str) -> None:
        """
        Switch to the named input.

        ``name`` accepts either form returned by :meth:`get_inputs`:

        - ``InputInfo.name`` — the cname-derived display label
          (e.g., ``"HDMI-1"``, ``"CAST"``).
        - ``InputInfo.meta_name`` — the device's canonical identifier
          (e.g., ``"HDMI-1"``, ``"SMARTCAST"``, or whatever the user
          renamed it to like ``"PS5"``).

        These differ for at least one stock input on every TV
        (``CAST`` / ``SMARTCAST``) and any input the user has renamed.
        We translate to the meta_name before sending — that's the form
        the device expects in the PUT body.

        Raises :class:`VizioInvalidInputError` if ``name`` matches no
        input, or if it ambiguously matches multiple inputs (e.g., the
        user renamed two HDMIs to ``"Living Room"``).
        """
        # Fetch inputs list and current_input directly — skip the public
        # get_inputs() because it would double-fetch current_input (it
        # uses the value to populate is_current, which we don't need here).
        inputs_response = await self._request(Endpoint.INPUTS)
        current_response = await self._request(Endpoint.CURRENT_INPUT)

        inputs = parse_inputs(inputs_response)
        target_cname = _resolve_input_target(name, inputs)
        target = next(i for i in inputs if i.cname == target_cname)

        current_item = parse_current_input_item(current_response)
        hashval = current_item.hashval if current_item.hashval is not None else 0

        # Short-circuit when already on the target input. Captured live
        # from VHD24M-0810 fw 3.720.9.1-1: setting current_input to the
        # value it already holds returns ``STATUS.RESULT="FAILURE"`` —
        # not a transient error, the device explicitly refuses to
        # "switch to the input it's already on." Treating that as a
        # no-op success matches user intent better than propagating a
        # misleading exception.
        #
        # The device's ``current_input.VALUE`` is inconsistent across
        # input types (display name for HDMI, meta_name for CAST), so
        # match against any of the three identifying forms.
        current_value = current_item.value
        candidates = {
            target.cname.lower(),
            target.name.lower(),
            target.meta_name.lower(),
        }
        if isinstance(current_value, str) and current_value.lower() in candidates:
            return
        if isinstance(current_value, Mapping):
            current_meta = current_value.get("name")
            if isinstance(current_meta, str) and current_meta.lower() in candidates:
                return

        # ``Endpoint.CURRENT_INPUT`` is declared GET in the table
        # (read-side dominates). For the write we override the method —
        # same pattern as ``_put_setting``. Without this override the PUT
        # body is silently dropped: the device returns success on the GET
        # and current_input doesn't change.
        spec = resolve(Endpoint.CURRENT_INPUT, self._profile)
        # Replace BOTH method and item_cname when switching to PUT. The
        # endpoint's GET response has an ``ITEMS`` entry with
        # ``cname='current_input'``, but the PUT acknowledgement
        # response carries only ``NAME`` and ``HASHVAL`` — no cname —
        # so leaving ``item_cname='current_input'`` set would make
        # ``_check_status`` raise VizioNotFoundError on the PUT's own
        # success response.
        spec_with_put = replace(spec, method="PUT", item_cname=None)
        # Device wants the lowercase cname in the PUT body — verified
        # live; display name returns FAILURE, meta_name returns
        # HASHVAL_ERROR. cname is the only stable identifier.
        body = _payloads.set_input(name=target_cname, hashval=hashval)
        await self._client.request_spec(spec_with_put, body=body)

    async def next_input(self) -> None:
        """Cycle to the next input (sends the ``INPUT_NEXT`` remote key)."""
        await self.send_key("INPUT_NEXT")

    # ------------------------------------------------------------------
    # Remote keys
    # ------------------------------------------------------------------

    async def send_key(self, key: str) -> None:
        """Send a single remote-key press."""
        code = self._profile.keymap.get(str(key))
        if code is None:
            raise VizioUnsupportedError(
                f"key {key!r} not in {self._profile.name} keymap"
            )
        await self._send_key_codes([code])

    async def send_text(self, text: str) -> None:
        """
        Type an ASCII string, e.g. into an on-screen search field.

        Each character is sent as a key in ``CODESET`` 0 whose ``CODE`` is
        the character's ASCII code point — the same encoding the official
        app's ``KeyCommandItem.ASCII_*`` keys use (``'A'`` → code 65,
        ``'5'`` → code 53, space → 32). The whole string goes out as one
        batched ``KEYLIST`` (chunked above :data:`_KEYLIST_CHUNK_SIZE`),
        so it lands in keystroke order.

        Raises :class:`VizioInvalidInputError` if any character is outside
        7-bit ASCII (code point > 127) — the device keymap has no codes for
        those. Empty ``text`` is a no-op.

        Note: this is the correct way to enter digits (e.g. for channel or
        PIN fields); ``'0'``-``'9'`` map to ASCII codes 48-57, *not* the
        ``RemoteKey.NUM_*`` codeset-0 codes 0-9 (which are ASCII control
        characters).
        """
        codes: list[tuple[int, int]] = []
        for ch in text:
            cp = ord(ch)
            if cp > 127:
                raise VizioInvalidInputError(
                    f"send_text supports ASCII only; {ch!r} (U+{cp:04X}) "
                    "has no key code"
                )
            codes.append((0, cp))
        for start in range(0, len(codes), _KEYLIST_CHUNK_SIZE):
            await self._send_key_codes(codes[start : start + _KEYLIST_CHUNK_SIZE])

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def get_setting_types(self) -> list[str]:
        """Return the top-level setting categories (e.g., audio, picture)."""
        response = await self._request(Endpoint.SETTINGS)
        return parse_setting_types(response)

    async def get_settings(self, setting_type: str) -> dict[str, SettingInfo]:
        """Return all settings under ``setting_type``, keyed by name, with options."""
        # Fetch values + options in parallel-ish (sequentially, through
        # the semaphore — but two GETs is the unavoidable shape).
        values_response = await self._request(
            Endpoint.SETTINGS, path_suffix=f"/{setting_type}"
        )
        try:
            options_response: Response | None = await self._request(
                Endpoint.SETTINGS_OPTIONS, path_suffix=f"/{setting_type}"
            )
        except VizioError:
            options_response = None
        return parse_settings(
            values_response, options_response, setting_type=setting_type
        )

    async def get_setting(self, setting_type: str, name: str) -> SettingInfo:
        """
        Fetch one setting plus its options.

        Static options (the ``options`` / ``min`` / ``max`` / ``center``
        fields of :class:`SettingInfo`) live at a separate static-tree
        endpoint. We fetch both — the dynamic value and the static
        options — and merge. The static fetch is best-effort: if the
        device doesn't expose a static counterpart for this leaf
        (returns ``URI_NOT_FOUND`` or otherwise errors), the returned
        :class:`SettingInfo` still has the value but with empty
        ``options`` / null bounds.
        """
        values_response = await self._request(
            Endpoint.SETTINGS, path_suffix=f"/{setting_type}/{name}"
        )
        try:
            options_response: Response | None = await self._request(
                Endpoint.SETTINGS_OPTIONS, path_suffix=f"/{setting_type}/{name}"
            )
        except VizioError:
            options_response = None
        # Reuse parse_settings (which knows how to merge value+options
        # into a SettingInfo) and pull out the matching entry. Slight
        # over-fetch when the response has multiple items, but the leaf
        # path's response has at most one in practice.
        merged = parse_settings(
            values_response, options_response, setting_type=setting_type
        )
        if name in merged:
            return merged[name]
        # Fall back to a value-only parse if the merge dropped it
        # (defensive — shouldn't happen for a well-formed leaf).
        item = values_response.require_item(name)
        return parse_setting(item, setting_type=setting_type)

    async def set_setting(
        self,
        setting_type: str,
        name: str,
        value: int | str,
        *,
        hashval: int | None = None,
    ) -> None:
        """
        MODIFY a setting.

        If ``hashval`` is supplied, sends one PUT — caller takes
        responsibility on stale-hashval failures and option-set
        validation. (Power-user path.)

        If not supplied, does GET-then-PUT. The GET also gives us the
        option set, so we validate the value client-side for list-type
        settings before issuing the PUT — gives a much better error
        message than the device's bare ``invalid_parameter`` (e.g., for
        ``dialogue_enhancer`` the canonical options are
        ``('Off', 'Low', 'Medium', 'High')`` — sending ``'On'`` would
        otherwise fail with no hint about valid values).

        On :class:`VizioInvalidParameterError` from the PUT (stale
        hashval race — protocol-notes #13), automatically retries once
        with a fresh hashval before propagating.
        """
        if hashval is not None:
            await self._put_setting(setting_type, name, value, hashval)
            return

        fresh = await self.get_setting(setting_type, name)
        value = _validate_setting_value(value, fresh)
        try:
            await self._put_setting(setting_type, name, value, fresh.hashval)
            return
        except VizioInvalidParameterError:
            # Hashval race — refetch and retry once.
            retry = await self.get_setting(setting_type, name)
            await self._put_setting(setting_type, name, value, retry.hashval)

    async def _put_setting(
        self, setting_type: str, name: str, value: int | str, hashval: int
    ) -> None:
        """Issue the raw MODIFY PUT for one setting leaf (no GET, no validation)."""
        await self._put_settings_body(
            setting_type, name, _payloads.write_setting(value=value, hashval=hashval)
        )

    async def _put_settings_body(
        self, setting_type: str, name: str, body: dict[str, Any]
    ) -> None:
        """PUT ``body`` at one settings leaf (the SETTINGS spec defaults to GET)."""
        spec = resolve(Endpoint.SETTINGS, self._profile)
        spec_with_put = replace(spec, method="PUT")
        await self._client.request_spec(
            spec_with_put,
            body=body,
            path_suffix=f"/{setting_type}/{name}",
        )

    async def trigger_setting_action(
        self,
        setting_type: str,
        name: str,
        *,
        hashval: int | None = None,
    ) -> None:
        """
        Fire a ``T_ACTION_V1`` settings item (``REQUEST: "ACTION"``).

        Action items are fire-and-forget menu entries — e.g.
        ``system/timers/blank_screen`` (see :meth:`blank_screen`) or
        ``admin_and_privacy/soft_power_cycle`` (reboot). They carry no
        real value; sending ``MODIFY`` at one returns ``PROXY_ERROR``.

        If ``hashval`` is supplied, sends one PUT — caller takes
        responsibility on stale-hashval failures. (Power-user path,
        same contract as :meth:`set_setting`.)

        If not supplied, does GET-then-PUT. The GET also lets us verify
        the leaf really is an action item — firing ``ACTION`` at a
        value leaf raises :class:`VizioInvalidInputError` client-side
        instead of whatever the device would do with it. On
        :class:`VizioInvalidParameterError` from the PUT (stale hashval
        race — protocol-notes #13), retries once with a fresh hashval.
        """
        if hashval is not None:
            await self._put_setting_action(setting_type, name, hashval)
            return

        fresh = await self._get_action_item(setting_type, name)
        try:
            await self._put_setting_action(setting_type, name, fresh)
        except VizioInvalidParameterError:
            # Hashval race — refetch and retry once.
            retry = await self._get_action_item(setting_type, name)
            await self._put_setting_action(setting_type, name, retry)

    async def _get_action_item(self, setting_type: str, name: str) -> int:
        """GET an action leaf, validate its type, and return its hashval."""
        response = await self._request(
            Endpoint.SETTINGS, path_suffix=f"/{setting_type}/{name}"
        )
        item = response.require_item(name)
        if item.type != SettingType.ACTION:
            raise VizioInvalidInputError(
                f"{setting_type}/{name} is {item.type!r}, not an action item"
                f" ({SettingType.ACTION.value}); use set_setting for values"
            )
        if item.hashval is None:
            # Malformed device response, not caller error — and a sibling
            # of VizioInvalidParameterError, so it can never be mistaken
            # for a hashval race by retry logic.
            raise VizioResponseError(
                f"{setting_type}/{name} returned no HASHVAL to echo"
            )
        return item.hashval

    async def _put_setting_action(
        self, setting_type: str, name: str, hashval: int
    ) -> None:
        """Issue the raw ACTION PUT for one action leaf (no GET, no validation)."""
        await self._put_settings_body(
            setting_type, name, _payloads.action_setting(hashval=hashval)
        )

    async def blank_screen(self) -> None:
        """
        Turn the panel off while audio and the device keep running.

        Fires the ``system/timers/blank_screen`` action — the same
        "Blank Screen" the TV menu offers and the 5-second mute-button
        hold triggers (Vizio support calls it "mute screen"; often
        called screen/display mute). One-way: firing it again keeps
        the panel dark, and there is no readable "blanked" state on the
        API. Any navigation keypress wakes the panel (verified:
        ``send_key("BACK")``); volume and mute keys intentionally do
        not, so audio stays adjustable while dark.

        TV-only: audio devices have no such menu leaf and raise
        :class:`VizioNotFoundError`. Verified live on M65Q7-H1 fw
        1.720.9.1-1; see protocol-notes #29.
        """
        await self.trigger_setting_action("system/timers", "blank_screen")

    # ------------------------------------------------------------------
    # Apps
    # ------------------------------------------------------------------

    async def get_current_app(self) -> str | None:
        """
        Return the running app's name, or ``None`` if no app is running.

        Migration: pyvizio returned ``NO_APP_RUNNING`` sentinel string;
        we return ``None`` for cleaner Python idioms. Use
        :data:`vizaio.apps.NO_APP_RUNNING` if the sentinel form
        is desired downstream.

        Resolution prefers availability data (the modern catalog ships
        no per-app launch config; ids must be joined to availability),
        and falls back to the legacy ``config[]`` field on archived
        catalog dumps for backward compatibility.
        """
        config = await self.get_current_app_config()
        if config is None:
            return None
        catalog = await self._get_app_catalog()
        availability = await self._get_app_availability()
        name = find_app_name(config, [APP_HOME, *catalog], availability=availability)
        if name == NO_APP_RUNNING:
            return None
        if name is None:
            return UNKNOWN_APP
        return name

    # ------------------------------------------------------------------
    # Bulk state poll (state_extended)
    # ------------------------------------------------------------------

    async def get_state_extended(self) -> StateExtended:
        """
        Fetch the device's aggregate state in one HTTP round trip.

        Returns power, current app, current input, screen mode, and
        media state — meaningfully cheaper than five individual GETs
        for HA-style polling integrations. Verified live on
        VHD24M-0810 fw 3.720.9.1-1; advertised under
        ``deviceinfo.scpl_capabilities.state_extended``.

        The on-the-wire response uses a different envelope shape
        (flat top-level keys, no ``STATUS``/``ITEMS``), so this method
        bypasses :class:`Response` and parses the raw JSON directly.
        Devices that don't expose the endpoint return ``URI_NOT_FOUND``
        which we surface as :class:`VizioNotFoundError`. Older firmware
        without the endpoint advertised may return a 500 (lighttpd
        "unhandled exception") — we let that propagate as
        :class:`VizioConnectionError`.
        """
        spec = resolve(Endpoint.STATE_EXTENDED, self._profile)
        payload = await self._client.request_raw_json(spec)
        # The endpoint reports its own ``ERRORS`` array — usually empty,
        # but if the device populates it we still hand back a typed
        # snapshot (errors live on ``StateExtended.errors`` for caller
        # inspection).
        return parse_state_extended(payload)

    async def get_versions(self) -> SystemVersions:
        """
        Fetch firmware/identity/component versions in one round trip.

        ``GET /system/versions`` returns firmware, serial number, ESN and
        a per-component version map (SCPL, ACR, AppleTV, …) — a cleaner,
        single-call source than the per-field ``menu_native``
        ``system_information`` scrapes (:meth:`get_device_info`,
        ``ESN``/``serial``/``version`` endpoints). Pairs well with
        :meth:`get_state_extended` for one-shot device snapshots.

        The wire envelope nests the data under ``ITEM.VALUE`` with
        device-cased keys (some with spaces); this method parses the raw
        JSON directly. Common fields are typed on :class:`SystemVersions`;
        the full map is on ``.raw``.
        """
        spec = resolve(Endpoint.SYSTEM_VERSIONS, self._profile)
        payload = await self._client.request_raw_json(spec)
        return parse_system_versions(payload)

    async def is_pin_default(self) -> bool:
        """
        Return whether the parental/purchase PIN is still factory-default.

        ``GET /pin/is_pin_default`` — ``True`` means no custom PIN has been
        set (purchases / parental-control screens are unprotected); ``False``
        means the user has set one. Verified live on VHD24M-0810: the device
        returns ``{"ITEM": {"VALUE": true}}`` (a JSON boolean).

        Read-only status check. vizaio deliberately does not expose
        ``set_pin`` / ``confirm_pin`` — setting a parental PIN is a
        lockout-risky write whose body semantics aren't pinned down from the
        APK — nor ``pairing/unpair`` (a vestigial route the official app
        never calls).
        """
        spec = resolve(Endpoint.PIN_IS_DEFAULT, self._profile)
        payload = await self._client.request_raw_json(spec)
        item = next((v for k, v in payload.items() if str(k).lower() == "item"), None)
        if isinstance(item, Mapping):
            value = next(
                (v for k, v in item.items() if str(k).lower() == "value"), None
            )
            return bool(value)
        return False

    async def get_current_app_config(self) -> AppConfig | None:
        """Return the active app's launch config, or ``None`` when no app is running."""
        if not self._profile.has_apps:
            raise VizioUnsupportedError(
                f"{self._profile.name} does not support SmartCast apps"
            )
        response = await self._request(Endpoint.CURRENT_APP)
        return parse_current_app_config(response)

    async def launch_app(
        self,
        name: str,
        *,
        chipset: str | None = None,
        firmware: str | None = None,
    ) -> None:
        """
        Launch an app by name (case-insensitive).

        Resolution order:

        1. Catalog record's legacy ``config[0]`` if populated (archived
           catalog dumps; the live ``scfs.vizio.com`` endpoint won't
           supply it).
        2. Availability data — picks the chipset+firmware-matched
           launch payload for this device.

        ``chipset`` and ``firmware`` override the auto-detected device
        values for the availability lookup. Pass them when you want to
        launch using a different variant's payload (e.g., to test the
        wildcard payload explicitly: ``chipset="*"``).

        Raises :class:`VizioInvalidParameterError` when the name isn't
        in the catalog, or when no availability payload matches the
        chipset/firmware combination in use.
        """
        catalog = await self._get_app_catalog()
        if APP_HOME.name.lower() == name.lower():
            await self.launch_app_config(APP_HOME.config[0])
            return
        record = find_app_record(name, catalog)
        if record is None:
            raise VizioInvalidParameterError(f"app {name!r} not in catalog")
        if record.config:
            await self.launch_app_config(record.config[0])
            return
        config = await self._resolve_launch_config(
            record, chipset=chipset, firmware=firmware
        )
        if config is None:
            raise VizioInvalidParameterError(
                f"app {name!r} is not available on this device "
                f"(chipset/firmware mismatch in availability data)"
            )
        await self.launch_app_config(config)

    async def list_apps(self) -> list[AppRecord]:
        """
        Return the full app catalog with no filtering.

        Use :meth:`list_available_apps` instead when you want only the
        apps that work on the current device. This method is the
        escape hatch for callers that need every catalog entry —
        e.g., to render a "Browse all apps" UI regardless of what
        the device can actually launch.
        """
        return list(await self._get_app_catalog())

    async def list_available_apps(
        self,
        *,
        country: str | None = None,
        chipset: str | None = None,
        firmware: str | None = None,
    ) -> list[AppRecord]:
        """
        List apps available given a chipset + firmware filter.

        - ``country``: ISO code to filter on (case-insensitive); ``None``
          (default) returns all available apps regardless of region.
        - ``chipset``: explicit availability chipset key
          (e.g., ``"MT5586"``); ``None`` (default) auto-detects from
          the device. Pass ``"*"`` to filter to apps whose availability
          ships a wildcard payload only.
        - ``firmware``: explicit firmware version string
          (e.g., ``"3.520.0.0-1"``); ``None`` (default) auto-detects.
          Pass ``""`` to skip firmware-bounds enforcement entirely
          (matches the permissive policy applied when the device
          doesn't expose ``SYSTEM_INFO.VERSION``).

        Inclusion rules, after the country filter:

        - records with a non-empty legacy ``config`` are included
          unconditionally (archived catalogs that ship launch payloads
          inline don't depend on availability data being joinable).
        - otherwise, the record's ``id`` must join an
          :class:`AppAvailability` entry whose chipset+firmware
          matches the resolved values; non-joinable records are
          excluded.

        Use :meth:`list_apps` for an unfiltered catalog.
        """
        catalog = await self._get_app_catalog()
        availability = await self._get_app_availability()
        resolved_chipset = chipset if chipset is not None else await self._get_chipset()
        resolved_firmware = (
            firmware if firmware is not None else await self._get_firmware()
        )
        # Build a one-shot id→availability index so the inner loop
        # is O(1) per record instead of scanning the availability
        # tuple every iteration. The catalog has ~350 entries today
        # but HA polls hard enough that the saved scan time matters.
        availability_by_id = {entry.app_id: entry for entry in availability}
        result: list[AppRecord] = []
        for record in catalog:
            if not app_in_country(record, country):
                continue
            # Backward-compat: archived catalog dumps with a populated
            # legacy ``config[]`` are launchable directly without
            # availability data. Include them so callers using older
            # bundled snapshots still get a sensible list.
            if record.config:
                result.append(record)
                continue
            entry = availability_by_id.get(record.id) if record.id else None
            if entry is None:
                continue
            if pick_chipset_payload(
                entry, chipset=resolved_chipset, firmware=resolved_firmware
            ):
                result.append(record)
        return result

    async def _resolve_launch_config(
        self,
        record: AppRecord,
        *,
        chipset: str | None = None,
        firmware: str | None = None,
    ) -> AppConfig | None:
        """
        Find the launch payload for ``record`` via availability data.

        ``chipset`` / ``firmware`` override the auto-detected device
        values. ``None`` triggers auto-detection (the common case).
        """
        if not record.id:
            return None
        availability = await self._get_app_availability()
        entry = find_availability(record.id, availability)
        if entry is None:
            return None
        resolved_chipset = chipset if chipset is not None else await self._get_chipset()
        resolved_firmware = (
            firmware if firmware is not None else await self._get_firmware()
        )
        payload = pick_chipset_payload(
            entry, chipset=resolved_chipset, firmware=resolved_firmware
        )
        return payload.config if payload else None

    async def get_chipset(self) -> str | None:
        """
        Return the device's availability chipset key (e.g., ``"MT5583"``).

        Derived from ``BINARIES.ViziOS`` in the deviceinfo response,
        cross-referenced with the chipset keys present in the active
        availability data so the ``MT5586``/``MT5586L`` ambiguity
        resolves to whichever variant the data actually carries.
        Cached per-instance.

        Returns ``None`` when the device doesn't expose the binary
        string (older firmware) or when the prefix doesn't match any
        known SoC vendor — callers should fall back to the wildcard
        ``"*"`` chipset entry, which :func:`pick_chipset_payload`
        already does automatically.
        """
        return await self._get_chipset()

    async def get_firmware_version(self) -> str:
        """
        Return the device's firmware version (e.g., ``"3.720.9.1-1"``).

        Read from ``SYSTEM_INFO.VERSION`` in the deviceinfo aggregate
        and cached per-instance. This is distinct from
        :meth:`get_version`, which hits the legacy per-field path
        (``firmware`` cname) and is what users print — both should
        agree on modern firmware. Empty string when the device
        doesn't expose it.
        """
        return await self._get_firmware()

    async def launch_app_config(self, config: AppConfig) -> None:
        """Launch a specific :class:`AppConfig` (low-level; see :meth:`launch_app`)."""
        if not self._profile.has_apps:
            raise VizioUnsupportedError(
                f"{self._profile.name} does not support SmartCast apps"
            )
        body = _payloads.launch_app(config)
        await self._client.request_spec(
            resolve(Endpoint.LAUNCH_APP, self._profile), body=body
        )

    def set_app_catalog(self, apps: tuple[AppRecord, ...]) -> None:
        """
        Replace the cached app catalog with caller-provided data.

        After this call the lib treats ``apps`` as authoritative and never
        auto-fetches — the caller (e.g., a Home Assistant apps coordinator)
        owns refresh. Mirrors the constructor's ``apps=`` injection point
        for callers that want to push fresh data post-construction.
        """
        self._cached_apps = apps
        self._cached_apps_at = datetime.now()
        self._caller_owned_apps = True

    def set_app_availability(self, availability: tuple[AppAvailability, ...]) -> None:
        """
        Replace the cached availability data with caller-provided data.

        Same caller-owned semantics as :meth:`set_app_catalog`.
        """
        self._cached_availability = availability
        self._cached_availability_at = datetime.now()
        self._caller_owned_availability = True

    # ------------------------------------------------------------------
    # Device info
    # ------------------------------------------------------------------

    async def get_model_name(self) -> str:
        """Return the display model (TV ``MODEL_NAME``; non-TV friendly ``NAME``)."""
        response = await self._get_deviceinfo()
        return parse_model_name(
            response, settings_root=self._profile.settings_root.value
        )

    async def get_esn(self) -> str:
        """Return the device's ESN; empty string if not exposed."""
        return await self._identity_field("esn")

    async def get_serial_number(self) -> str:
        """Return the device's hardware serial number; empty string if not exposed."""
        return await self._identity_field("serial_number")

    async def get_version(self) -> str:
        """Return the device's firmware/version string; empty if not exposed."""
        # Modern firmware (~3.7+) exposes the version under cname='firmware'
        # in the aggregate; older firmware exposes it under cname='version'
        # at a per-field child path. _identity_field tries both forms.
        return await self._identity_field("version", aliases=("firmware",))

    async def _identity_field(
        self, cname: str, *, aliases: tuple[str, ...] = ()
    ) -> str:
        """
        Read one identity field, preferring the aggregate endpoint.

        Resolution order:

        1. The unauthenticated ``state/device/deviceinfo`` payload, for
           the fields it carries under SYSTEM_INFO (``serial_number``,
           ``version``). Reading these here means they work with **no
           auth token** and skip the auth-gated ``menu_native`` round
           trip entirely. ESN has no deviceinfo equivalent, so it never
           takes this path.
        2. The aggregate ``.../tv_information`` envelope (auth-gated on
           TVs), which modern firmware (verified VHD24M-0810 fw
           3.720.9.1-1) returns in one shot — the per-field child paths
           like ``.../tv_information/esn`` return ``URI_NOT_FOUND``.
        3. The per-field endpoints, for older firmware that doesn't
           expose the aggregate at all.
        """
        if cname in _DEVICEINFO_IDENTITY_FIELDS:
            system_info = await self._get_system_info()
            # Walk cname + aliases, same as the aggregate branch below, so
            # a firmware that keys the value under an alias (e.g. version
            # under ``firmware``) is still read from deviceinfo instead of
            # silently falling through to the auth-gated path.
            for key in (cname, *aliases):
                value = system_info.get(key)
                if value:
                    return str(value)

        info = await self._get_identity_aggregate()
        if info is not None:
            for key in (cname, *aliases):
                value = info.get(key)
                if value:
                    return value
        # Aggregate didn't carry the field (or wasn't exposed). Try the
        # per-field endpoint with its built-in firmware-fallback paths.
        per_field_endpoint = _PER_FIELD_IDENTITY_ENDPOINTS.get(cname)
        if per_field_endpoint is None:
            return ""
        try:
            response = await self._request(per_field_endpoint)
        except _IDENTITY_FALLTHROUGH:
            return ""
        item = response.find_item(cname)
        return str(item.value or "") if item is not None else ""

    async def _get_identity_aggregate(self) -> dict[str, str] | None:
        """
        Return ``cname → value`` mapping for the aggregate identity endpoint.

        Fetches ``Endpoint.TV_INFORMATION`` and caches the result on the
        instance — identity is immutable for the device lifetime.

        Returns ``None`` (and caches that decision) when the aggregate
        is not available on this firmware, so subsequent calls don't
        keep trying.
        """
        if self._cached_identity_loaded:
            return self._cached_identity
        self._cached_identity_loaded = True
        try:
            response = await self._request(Endpoint.TV_INFORMATION)
        except _IDENTITY_FALLTHROUGH:
            self._cached_identity = None
            return None
        self._cached_identity = {
            item.cname: str(item.value)
            for item in response.items
            if item.value is not None
        }
        return self._cached_identity

    async def get_device_info(self) -> DeviceInfo:
        """
        Aggregate device identity in one method call.

        Internally fires several requests sequentially through the
        semaphore. Individual failures don't fail the aggregate — fields
        we couldn't fetch return as empty string / empty tuple.
        """

        async def _safe(coro: Any, default: Any) -> Any:
            """Await ``coro``, returning ``default`` on any :class:`VizioError`."""
            try:
                return await coro
            except VizioError:
                return default

        model = await _safe(self.get_model_name(), "")
        serial = await _safe(self.get_serial_number(), "")
        esn = await _safe(self.get_esn(), "")
        version = await _safe(self.get_version(), "")

        inputs: tuple[InputInfo, ...] = ()
        if self._profile.has_inputs:
            with contextlib.suppress(VizioError):
                inputs = tuple(await self.get_inputs())

        return DeviceInfo(
            model=model,
            serial_number=serial,
            esn=esn,
            version=version,
            inputs=inputs,
        )

    # ------------------------------------------------------------------
    # Battery (Crave-only — resolver enforces)
    # ------------------------------------------------------------------

    async def get_battery_level(self) -> int:
        """Return the device's battery level percentage (Crave only)."""
        if not self._profile.has_battery:
            raise VizioUnsupportedError(f"{self._profile.name} does not have a battery")
        response = await self._request(Endpoint.BATTERY_LEVEL)
        return int(response.require_item("battery_level").value)

    async def get_charging_status(self) -> ChargingStatus:
        """Return the device's :class:`ChargingStatus` (Crave only)."""
        if not self._profile.has_battery:
            raise VizioUnsupportedError(f"{self._profile.name} does not have a battery")
        response = await self._request(Endpoint.CHARGING_STATUS)
        return ChargingStatus(int(response.require_item("charging_status").value))

    # ------------------------------------------------------------------
    # Pairing — stateless one-shot methods
    # ------------------------------------------------------------------

    async def begin_pair(self, *, device_id: str, device_name: str) -> PairChallenge:
        """
        Initiate a pairing handshake.

        Returns the challenge data needed to complete the handshake via
        :meth:`finish_pair`.
        """
        body = _payloads.begin_pair(device_id=device_id, device_name=device_name)
        response = await self._client.request_spec(
            resolve(Endpoint.BEGIN_PAIR, self._profile), body=body
        )
        return parse_pair_challenge(response)

    async def finish_pair(
        self, *, device_id: str, challenge: PairChallenge, pin: str
    ) -> str:
        """
        Complete a pairing handshake with the user-provided PIN.

        ``challenge`` is the :class:`PairChallenge` returned by
        :meth:`begin_pair`. Returns the auth token on success.
        """
        body = _payloads.finish_pair(device_id=device_id, challenge=challenge, pin=pin)
        response = await self._client.request_spec(
            resolve(Endpoint.FINISH_PAIR, self._profile), body=body
        )
        return parse_auth_token(response)

    async def cancel_pair(self, *, device_id: str, device_name: str) -> None:
        """
        Abort an in-progress pairing handshake.

        Best-effort — errors are suppressed so a failed cancel never
        masks the caller's original exception.
        """
        body = _payloads.cancel_pair(device_id=device_id, device_name=device_name)
        with contextlib.suppress(VizioError):
            await self._client.request_spec(
                resolve(Endpoint.CANCEL_PAIR, self._profile), body=body
            )

    def pair_session(self, *, device_id: str, device_name: str) -> PairSession:
        """
        Open a pairing session.

        Use as an async context manager; auto-cancels on error::

            async with vizio.pair_session(
                device_id="ha-coord", device_name="HomeAssistant"
            ) as session:
                token = await session.complete(pin="1234")
        """
        return PairSession(self, device_id=device_id, device_name=device_name)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _request(self, endpoint: Endpoint, *, path_suffix: str = "") -> Response:
        """Resolve ``endpoint`` against this profile, check auth, issue the GET."""
        spec = resolve(endpoint, self._profile)
        self._check_auth(spec.auth)
        return await self._client.request_spec(spec, path_suffix=path_suffix)

    def _check_auth(self, requirement: AuthRequirement) -> None:
        """
        Fail-fast at the Vizio level if auth is required but missing.

        ``SmartCastClient`` does its own check, but mirroring it here means
        callers of public ``Vizio`` methods see ``VizioAuthError`` even
        when ``request_spec`` is replaced (e.g., test code mocking the
        client to inspect call args).
        """
        if requirement is AuthRequirement.REQUIRED and not self._auth_token:
            raise VizioAuthError(
                "auth token required for this endpoint but none configured"
            )

    async def _send_repeated_key(self, key: str, steps: int) -> None:
        """
        Send a single key repeated N times.

        Per protocol-notes #19, the official app sends arbitrary-length
        KEYLISTs in one PUT. We chunk only above ``_KEYLIST_CHUNK_SIZE``
        as defensive insurance against worst-case device buffers.
        """
        if steps < 1:
            return
        code = self._profile.keymap.get(key)
        if code is None:
            raise VizioUnsupportedError(
                f"key {key!r} not in {self._profile.name} keymap"
            )
        remaining = steps
        while remaining > 0:
            chunk_size = min(remaining, _KEYLIST_CHUNK_SIZE)
            await self._send_key_codes([code] * chunk_size)
            remaining -= chunk_size

    async def _send_key_codes(self, codes: Sequence[tuple[int, int]]) -> None:
        """PUT one ``KEYLIST`` payload of ``(codeset, code)`` pairs."""
        body = _payloads.key_press(codes)
        await self._client.request_spec(
            resolve(Endpoint.KEY_PRESS, self._profile),
            body=body,
        )

    async def _get_app_catalog(self) -> tuple[AppRecord, ...]:
        """
        Return the cached app catalog, refreshing if older than the TTL.

        When the caller injected an ``apps=`` value via the constructor,
        that value is treated as authoritative and never refetched —
        the caller (e.g., a Home Assistant coordinator) is taking
        responsibility for refresh.
        """
        if self._caller_owned_apps and self._cached_apps is not None:
            return self._cached_apps
        now = datetime.now()
        if (
            self._cached_apps is not None
            and self._cached_apps_at is not None
            and now - self._cached_apps_at < _APP_CATALOG_TTL
        ):
            return self._cached_apps
        catalog = await fetch_app_catalog()
        self._cached_apps = catalog or BUNDLED_APPS
        self._cached_apps_at = now
        return self._cached_apps

    async def _get_app_availability(self) -> tuple[AppAvailability, ...]:
        """
        Return cached availability data, refreshing if older than the TTL.

        Caller-injected values (constructor's ``availability=``) are
        treated as authoritative and never refetched.
        """
        if self._caller_owned_availability and self._cached_availability is not None:
            return self._cached_availability
        now = datetime.now()
        if (
            self._cached_availability is not None
            and self._cached_availability_at is not None
            and now - self._cached_availability_at < _APP_AVAILABILITY_TTL
        ):
            return self._cached_availability
        availability = await fetch_app_availability()
        self._cached_availability = availability or BUNDLED_AVAILABILITY
        self._cached_availability_at = now
        return self._cached_availability

    async def _get_chipset(self) -> str | None:
        """Lazily read + cache the device's availability chipset key."""
        if self._cached_chipset is None:
            await self._load_deviceinfo_fields()
        return self._cached_chipset or None

    async def _get_firmware(self) -> str:
        """Lazily read + cache the device's firmware version string."""
        if self._cached_firmware is None:
            await self._load_deviceinfo_fields()
        return self._cached_firmware or ""

    async def _load_deviceinfo_fields(self) -> None:
        """
        Populate cached chipset + firmware from a single deviceinfo fetch.

        Both fields come from the same response payload, so doing them in
        one round-trip keeps ``list_available_apps`` and the
        availability-aware ``launch_app`` from issuing two back-to-back
        ``state/device/deviceinfo`` GETs on first use. Empty string is
        the "fetched but field absent / device unreachable" sentinel —
        once set, neither field will trigger another fetch.
        """
        try:
            response = await self._get_deviceinfo()
        except VizioError:
            if self._cached_chipset is None:
                self._cached_chipset = ""
            if self._cached_firmware is None:
                self._cached_firmware = ""
            return
        binary = parse_vizios_binary(response)
        availability = await self._get_app_availability()
        keys = {k for entry in availability for k in entry.chipsets}
        self._cached_chipset = extract_chipset(binary, available_keys=keys) or ""
        self._cached_firmware = parse_firmware_version(response)

    async def _get_deviceinfo(self) -> Response:
        """
        Fetch (and cache for the session) the deviceinfo response.

        Shared by :meth:`get_model_name`, the chipset/firmware probe, and
        the serial/version identity reads so a single unauthenticated
        ``state/device/deviceinfo`` GET serves all of them. deviceinfo is
        immutable for the device lifetime, so the *outcome* is cached
        either way: a success is reused, and a failure is remembered in
        ``_cached_deviceinfo_error`` and re-raised on subsequent calls
        rather than re-fetched. This matches the one-attempt-per-session
        negative caching of :meth:`_get_identity_aggregate` and
        :meth:`_load_deviceinfo_fields`, and stops a single unreachable
        deviceinfo from turning one :meth:`get_device_info` into three
        failing GETs (model + serial + version) or a poll loop into a
        request storm. The lock coalesces concurrent callers (e.g.
        ``asyncio.gather(get_model_name(), get_version())``) so they
        issue one GET, not two. Callers that want best-effort behavior
        wrap this in ``try/except VizioError``.
        """
        async with self._deviceinfo_lock:
            if self._cached_deviceinfo_error is not None:
                raise self._cached_deviceinfo_error
            if self._cached_deviceinfo is None:
                try:
                    self._cached_deviceinfo = await self._request(Endpoint.DEVICE_INFO)
                except VizioError as err:
                    self._cached_deviceinfo_error = err
                    raise
            return self._cached_deviceinfo

    async def _get_system_info(self) -> Mapping[str, Any]:
        """
        Return the SYSTEM_INFO block from the shared deviceinfo response.

        Empty mapping when deviceinfo is unreachable or the firmware
        doesn't expose the block — callers fall back to the auth-gated
        identity endpoints.
        """
        try:
            response = await self._get_deviceinfo()
        except _IDENTITY_FALLTHROUGH:
            return {}
        return parse_system_info(response)


def _validate_setting_value(value: int | str, info: SettingInfo) -> int | str:
    """
    Validate (and where possible, canonicalize) a value before a setting PUT.

    For list-type settings (``T_LIST_V1``, ``T_LIST_X_V1``) with a
    populated ``options`` set: case-insensitively match the value
    against the options. If a match exists, return the canonical-case
    option string (so ``'on'`` → ``'On'``). If not, raise
    :class:`VizioInvalidParameterError` listing the valid options —
    much more useful than the device's bare ``invalid_parameter``
    response.

    For numeric (T_VALUE_V1 / T_VALUE_ABS_V1) settings with declared
    bounds: range-check. Out-of-range values raise.

    For settings without options or bounds (or unknown types), pass
    through unchanged.
    """
    is_list = info.type in (SettingType.LIST, SettingType.LIST_X)
    if is_list and info.options and isinstance(value, str):
        wanted = value.lower()
        for option in info.options:
            if option.lower() == wanted:
                return option
        raise VizioInvalidParameterError(
            f"value {value!r} not in options for "
            f"{info.setting_type}.{info.name}; "
            f"valid: {list(info.options)}"
        )

    if info.type in (SettingType.INT, SettingType.SLIDER) and isinstance(value, int):
        if info.min is not None and value < info.min:
            raise VizioInvalidParameterError(
                f"value {value} below min={info.min} for "
                f"{info.setting_type}.{info.name}"
            )
        if info.max is not None and value > info.max:
            raise VizioInvalidParameterError(
                f"value {value} above max={info.max} for "
                f"{info.setting_type}.{info.name}"
            )

    return value


def _resolve_input_target(name: str, inputs: Sequence[InputInfo]) -> str:
    """
    Resolve a user-supplied input identifier to its canonical **cname**.

    Verified live: the device's ``current_input`` PUT body carries the
    lowercase ``cname`` (e.g., ``"hdmi2"``), **not** the display name
    (``"HDMI-2"``) or the meta_name (``"Mac"``). Sending the wrong form
    returns ``FAILURE`` (display name) or ``HASHVAL_ERROR`` (meta_name).
    To accept all three forms transparently we map them back to the
    cname here.

    Resolution order, all case-insensitive:

    1. Exact ``cname`` match — most specific.
    2. Exact ``meta_name`` match — handles the ``CAST`` ↔ ``SMARTCAST``
       case and any user rename.
    3. Exact ``name`` match — display label fallback.

    Raises :class:`VizioInvalidInputError` when no input matches, or
    when matching is ambiguous (e.g., the user renamed two HDMIs to
    the same label).
    """
    target = name.lower()
    candidates_by = {
        "cname": [i for i in inputs if i.cname.lower() == target],
        "meta_name": [i for i in inputs if i.meta_name.lower() == target],
        "name": [i for i in inputs if i.name.lower() == target],
    }
    for label, hits in candidates_by.items():
        if len(hits) == 1:
            return hits[0].cname
        if len(hits) > 1:
            raise VizioInvalidInputError(
                f"input {name!r} matches multiple inputs by {label} "
                f"({[i.cname for i in hits]}); rename one to disambiguate"
            )

    valid = sorted(
        {i.cname for i in inputs}
        | {i.name for i in inputs}
        | {i.meta_name for i in inputs if i.meta_name}
    )
    raise VizioInvalidInputError(f"input {name!r} not found. Valid: {valid}")


class PairSession:
    """
    Async context manager that wraps the pairing handshake.

    Auto-cancels on exception, on early exit, or when ``complete()`` is
    never called. Does NOT cancel on successful complete.
    """

    def __init__(
        self,
        vizio: Vizio,
        *,
        device_id: str,
        device_name: str,
    ) -> None:
        """Stash the pairing identity; the handshake fires on ``__aenter__``."""
        self._vizio = vizio
        self._device_id = device_id
        self._device_name = device_name
        self._challenge: PairChallenge | None = None
        self._completed = False

    @property
    def challenge(self) -> PairChallenge:
        """Return the :class:`PairChallenge`; raises before context entry."""
        if self._challenge is None:
            raise RuntimeError("PairSession not entered yet — use 'async with'")
        return self._challenge

    async def __aenter__(self) -> Self:
        """Begin the pairing handshake; auto-cancels if the begin call itself raises."""
        try:
            self._challenge = await self._vizio.begin_pair(
                device_id=self._device_id, device_name=self._device_name
            )
        except BaseException:
            await self._vizio.cancel_pair(
                device_id=self._device_id, device_name=self._device_name
            )
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Cancel the pairing on exit unless ``complete()`` already finalized it."""
        if self._completed:
            return
        # cancel_pair is best-effort — it swallows VizioError internally
        # so a failed cancel never masks the caller's original exception.
        await self._vizio.cancel_pair(
            device_id=self._device_id, device_name=self._device_name
        )

    async def complete(self, *, pin: str) -> str:
        """
        Submit the user's PIN and return the auth token.

        Not idempotent: a second call raises ``RuntimeError`` (caller bug).
        """
        if self._completed:
            raise RuntimeError("PairSession.complete() already called")
        if self._challenge is None:
            raise RuntimeError("PairSession not entered yet")
        token = await self._vizio.finish_pair(
            device_id=self._device_id,
            challenge=self._challenge,
            pin=pin,
        )
        self._completed = True
        return token
