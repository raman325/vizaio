"""
Low-level HTTP transport.

Owns the aiohttp session and orchestrates requests against
:class:`EndpointSpec` objects. Responsibilities:

- URL construction: ``https://{host}{path}`` (SSL verification disabled
  — devices use self-signed certs).
- Auth header: ``AUTH: <token>`` for endpoints whose ``EndpointSpec.auth``
  is ``OPTIONAL`` and a token is configured, or ``REQUIRED``. Raises
  :class:`VizioAuthError` upfront if REQUIRED and no token set.
- Identity header: ``VIZIO-SmartCast-Source: vizaio`` (matches
  the pattern of the official app sending ``SMARTCAST_ANDROID``).
- Status validation: HTTP 200 + SmartCast ``STATUS.RESULT == success``,
  with per-result-code mapping to typed exceptions.
- Endpoint fallback: for endpoints with multiple paths (firmware
  variation), tries each in order, falling through to the next on
  ``VizioNotFoundError`` from the parser.

Default timeouts match the official Vizio Android app per APK findings:
read=10s, write=3s, connect=2s.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import logging
from typing import Any, Final

from aiohttp import (
    ClientConnectionError,
    ClientError,
    ClientResponse,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    TCPConnector,
)

from .endpoints import EndpointSpec
from .errors import (
    VizioAuthError,
    VizioBusyError,
    VizioConnectionError,
    VizioInvalidParameterError,
    VizioNotFoundError,
    VizioResponseError,
    VizioWifiError,
)
from .types import AuthRequirement, ResponseStatus, WifiResult
from .wire import Response

_LOGGER = logging.getLogger(__name__)

REDACTED: Final = "<redacted>"

# Radio/DHCP results from the Wi-Fi provisioning leaves. The prefix set
# catches NET_* strings we don't model yet — the device's vocabulary is
# wider than what the APK's constants file enumerates.
_WIFI_RESULT_VALUES: Final[set[str]] = {r.value for r in WifiResult}
_WIFI_RESULT_PREFIXES: Final[tuple[str, ...]] = ("net_wifi_", "net_ip_", "net_unknown")

DEFAULT_CONNECT_TIMEOUT: Final = 2.0
DEFAULT_WRITE_TIMEOUT: Final = 3.0
DEFAULT_READ_TIMEOUT: Final = 10.0

# Commands (PUT) get a longer response budget than reads. Sluggish or older
# hardware can take many seconds to acknowledge a key press, and a timed-out
# command surfaces to the user as "the button did nothing". Reads stay strict
# because they run on a polling loop where falling behind is worse than a
# stale value. Note this is unrelated to DEFAULT_WRITE_TIMEOUT above, which
# describes time to *send* bytes, not to await the device's reply.
DEFAULT_COMMAND_TIMEOUT: Final = 30.0

HEADER_AUTH: Final = "AUTH"
HEADER_SOURCE: Final = "VIZIO-SmartCast-Source"
SOURCE_IDENTIFIER: Final = "vizaio"


class SmartCastClient:
    """
    Async HTTP transport for a single SmartCast device.

    Owns or shares an :class:`aiohttp.ClientSession`. When constructed
    with ``session=None``, creates and owns a session, closing it in
    :meth:`aclose`. When given a session, never closes it — caller's
    responsibility.
    """

    def __init__(
        self,
        host: str,
        *,
        auth_token: str | None = None,
        session: ClientSession | None = None,
        timeout: float | ClientTimeout | None = None,
        command_timeout: float | ClientTimeout | None = None,
        max_concurrent: int = 1,
    ) -> None:
        """
        Initialize the client.

        ``session`` is owned (created lazily, closed in :meth:`aclose`) when
        ``None``; otherwise the caller's session is borrowed and never closed.
        """
        self._host = host
        self._auth_token = auth_token
        self._session = session
        self._owns_session = session is None
        self._timeout = _coerce_timeout(timeout)
        self._command_timeout = _coerce_timeout(
            command_timeout, default=DEFAULT_COMMAND_TIMEOUT
        )
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._closed = False

    async def request_spec(
        self,
        spec: EndpointSpec,
        *,
        body: Mapping[str, Any] | None = None,
        path_suffix: str = "",
    ) -> Response:
        """
        Issue the request described by ``spec``.

        ``path_suffix`` is appended to each path in ``spec.paths`` —
        useful for settings endpoints where the spec describes the
        category root and the caller adds the specific setting name.

        Returns the parsed :class:`Response` from the first path that
        produces a non-:class:`VizioNotFoundError` result. Other errors
        propagate immediately.
        """
        self._check_auth(spec.auth)

        last_not_found: VizioNotFoundError | None = None
        for path in spec.paths:
            full_path = path + path_suffix if path_suffix else path
            try:
                return await self._do_request(
                    method=spec.method, path=full_path, body=body, spec=spec
                )
            except VizioNotFoundError as e:
                last_not_found = e
                continue
        # All paths failed with NotFound — raise the last error so
        # callers see a meaningful endpoint reference.
        assert last_not_found is not None
        raise last_not_found

    async def request_raw_json(
        self,
        spec: EndpointSpec,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """
        Issue a request and return the raw parsed JSON without envelope validation.

        For SCPL endpoints whose response shape doesn't match the
        standard ``STATUS``/``ITEMS`` envelope. Currently the only such
        endpoint is ``/state_extended`` — flat keys, no envelope. The
        caller is responsible for shape validation.

        Auth, headers, SSL, and the request semaphore are reused from
        :meth:`request_spec`. Endpoint-multi-path fallback is **not**
        applied here — non-standard endpoints are typically single-path.
        """
        self._check_auth(spec.auth)
        if not spec.paths:
            raise ValueError("EndpointSpec has no paths")
        path = spec.paths[0]

        session = await self._ensure_session()
        url = f"https://{self._host}{path}"
        headers = self._build_headers(spec)
        try:
            async with self._semaphore:
                if spec.method == "GET":
                    async with session.get(url, headers=headers, ssl=False) as resp:
                        return await _read_raw_json(resp)
                payload = json.dumps(body or {})
                headers["Content-Type"] = "application/json"
                async with session.put(
                    url, data=payload, headers=headers, ssl=False
                ) as resp:
                    return await _read_raw_json(resp)
        except (TimeoutError, ClientConnectionError) as e:
            raise VizioConnectionError(f"failed to reach {url}: {e}") from e
        except ClientResponseError as e:
            raise VizioConnectionError(
                f"unexpected HTTP status {e.status} from {url}"
            ) from e
        except (ClientError, ValueError) as e:
            # Catches ``InvalidURL`` (yarl can't parse the host:port) and
            # any other aiohttp ``ClientError`` subclass not covered above.
            # ``ValueError`` covers cases where yarl raises directly without
            # going through aiohttp's wrapping. Without this, callers that
            # promise lenient semantics (e.g., ``async_is_tv``) would leak
            # transport-layer exception types.
            raise VizioConnectionError(f"client error reaching {url}: {e}") from e

    async def aclose(self) -> None:
        """Close the owned session, if any."""
        if self._closed:
            return
        self._closed = True
        if self._owns_session and self._session is not None:
            await self._session.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_auth(self, requirement: AuthRequirement) -> None:
        """Raise :class:`VizioAuthError` early if REQUIRED and no token is set."""
        if requirement is AuthRequirement.REQUIRED and not self._auth_token:
            raise VizioAuthError(
                "auth token required for this endpoint but none configured"
            )

    async def _ensure_session(self) -> ClientSession:
        """Lazily create the owned aiohttp session on first request."""
        if self._session is None:
            self._session = ClientSession(
                connector=TCPConnector(ssl=False),
                timeout=self._timeout,
            )
        return self._session

    def _build_headers(self, spec: EndpointSpec) -> dict[str, str]:
        """Build headers: source identifier always; AUTH when needed + available."""
        headers = {HEADER_SOURCE: SOURCE_IDENTIFIER}
        if spec.auth is not AuthRequirement.NONE and self._auth_token:
            headers[HEADER_AUTH] = self._auth_token
        return headers

    async def _do_request(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        spec: EndpointSpec,
    ) -> Response:
        """Issue the HTTP call for one resolved path; wraps transport errors."""
        session = await self._ensure_session()
        url = f"https://{self._host}{path}"
        headers = self._build_headers(spec)

        try:
            async with self._semaphore:
                if method == "GET":
                    _LOGGER.debug("GET %s headers=%s", url, _redact(headers))
                    async with session.get(url, headers=headers, ssl=False) as resp:
                        return await _parse_response(resp, spec)
                else:
                    payload = json.dumps(body or {})
                    headers["Content-Type"] = "application/json"
                    _LOGGER.debug(
                        "PUT %s headers=%s body=%s",
                        url,
                        _redact(headers),
                        _redact_body(body, sensitive=spec.sensitive),
                    )
                    async with session.put(
                        url,
                        data=payload,
                        headers=headers,
                        ssl=False,
                        timeout=self._command_timeout,
                    ) as resp:
                        return await _parse_response(resp, spec)
        except (
            VizioAuthError,
            VizioBusyError,
            VizioConnectionError,
            VizioInvalidParameterError,
            VizioNotFoundError,
            VizioResponseError,
        ):
            raise
        except (TimeoutError, ClientConnectionError) as e:
            raise VizioConnectionError(f"failed to reach {url}: {e}") from e
        except ClientResponseError as e:
            raise VizioConnectionError(
                f"unexpected HTTP status {e.status} from {url}"
            ) from e
        except (ClientError, ValueError) as e:
            raise VizioConnectionError(f"client error reaching {url}: {e}") from e


def _coerce_timeout(
    timeout: float | ClientTimeout | None,
    *,
    default: float = DEFAULT_READ_TIMEOUT,
) -> ClientTimeout:
    """Coerce a response-timeout float or ``None`` into a ``ClientTimeout``."""
    if isinstance(timeout, ClientTimeout):
        return timeout
    read = float(timeout) if timeout is not None else default
    return ClientTimeout(
        total=None,
        connect=DEFAULT_CONNECT_TIMEOUT,
        sock_connect=DEFAULT_CONNECT_TIMEOUT,
        sock_read=read,
    )


def _redact(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with the AUTH value masked for debug logs."""
    return {k: ("<redacted>" if k == HEADER_AUTH else v) for k, v in headers.items()}


def _redact_body(body: Mapping[str, Any] | None, *, sensitive: bool) -> Any:
    """
    Return a log-safe rendering of a request body.

    Two layers, because the password reaches the wire two different ways.
    ``sensitive`` endpoints (``set_wifi_password``) carry the secret in
    ``VALUE``, which is indistinguishable from any other setting write, so
    the whole body is replaced. Independently, any ``PASSWORD`` key is
    masked wherever it appears — that covers ``hidden_network``, whose
    body nests the secret inside ``VALUE[0]``, and anything added later
    that follows the same naming.
    """
    if body is None:
        return None
    if sensitive:
        return REDACTED
    return _mask_password_keys(body)


def _mask_password_keys(value: Any) -> Any:
    """Recursively replace any ``PASSWORD`` value with a placeholder."""
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if str(k).upper() == "PASSWORD" else _mask_password_keys(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_password_keys(v) for v in value]
    return value


async def _read_raw_json(resp: ClientResponse) -> Mapping[str, Any]:
    """Return parsed JSON from ``resp`` without envelope validation."""
    _check_http_status(resp)
    try:
        text = await resp.text()
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError) as e:
        raise VizioResponseError(f"failed to parse JSON: {e}") from e
    if not isinstance(data, Mapping):
        raise VizioResponseError(f"expected a JSON object, got {type(data).__name__}")
    return data


async def _parse_response(resp: ClientResponse, spec: EndpointSpec) -> Response:
    """Parse + envelope-validate a SmartCast response; raise typed errors per code."""
    _check_http_status(resp)
    try:
        text = await resp.text()
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError) as e:
        raise VizioResponseError(f"failed to parse JSON: {e}") from e

    response = Response.from_json(data)
    _check_status(response, spec)
    return response


def _check_http_status(resp: ClientResponse) -> None:
    """
    Validate HTTP status before reading the body.

    Auth-related rejections come back as raw HTTP 401/403 (not as
    SCPL envelope errors), so they need to surface as
    :class:`VizioAuthError` rather than :class:`VizioConnectionError`.
    Verified live on VHD24M-0810 fw 3.720.9.1-1: re-pairing with the
    same ``device_id`` invalidates the previous token, and subsequent
    calls with the old token return HTTP 403.
    """
    if resp.status == 200:
        return
    if resp.status in (401, 403):
        raise VizioAuthError(
            f"device returned HTTP {resp.status} — token rejected "
            f"(may have been invalidated by re-pair)"
        )
    raise VizioConnectionError(f"device returned HTTP {resp.status} for {resp.url}")


def _check_status(response: Response, spec: EndpointSpec) -> None:
    """Map :class:`ResponseStatus` to typed exceptions."""
    status = response.status
    if status is ResponseStatus.SUCCESS:
        # If the endpoint asks for a specific item, surface a NotFound
        # so the client falls through to alt paths.
        if spec.item_cname is not None and not response.has_item(spec.item_cname):
            raise VizioNotFoundError(
                f"response missing required item {spec.item_cname!r}"
            )
        return
    if status in (ResponseStatus.INVALID_PARAMETER, ResponseStatus.HASHVAL_ERROR):
        # HASHVAL_ERROR is a more specific form of "your write parameters
        # don't match the current state." Map it to the same exception
        # as INVALID_PARAMETER so existing hashval-race retry logic in
        # set_setting fires for both.
        raise VizioInvalidParameterError(
            response.detail or "device rejected request as invalid"
        )
    if status is ResponseStatus.URI_NOT_FOUND:
        # Modern firmware (~3.7+) replies HTTP 200 with this status for
        # paths it doesn't expose. Surface as VizioNotFoundError so that
        # ``request_spec``'s multi-path fallback can try alternates.
        raise VizioNotFoundError(
            response.detail
            or f"device has no endpoint at {response.uri or '<unknown>'}"
        )
    if status is ResponseStatus.BLOCKED:
        raise VizioBusyError(response.detail or "device blocked the request")
    if status in (
        ResponseStatus.REQUIRES_PAIRING,
        ResponseStatus.PAIRING_DENIED,
    ):
        raise VizioAuthError(response.detail or status.value)
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
    # FAILURE, UNKNOWN, anything else.
    raise VizioResponseError(
        f"unexpected status {response.result_raw!r}: {response.detail}"
    )
