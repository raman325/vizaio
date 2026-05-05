"""Additional ``SmartCastClient`` tests covering paths not exercised
by the existing transport tests.

Targets:
- ``request_raw_json`` (used for ``/state_extended``) — success, GET/PUT,
  body shape, transport errors, JSON-decode failure, non-object payload.
- ``_check_http_status`` — 401/403 → ``VizioAuthError``, non-200 →
  ``VizioConnectionError``.
- ``_check_status`` — BLOCKED, REQUIRES_PAIRING, PAIRING_DENIED, SUCCESS
  with missing required item.
- ``aclose`` idempotency.
- ``_check_auth`` — missing-token error.
- Borrowed-session lifecycle (caller's session is not closed by us).

All tests run end-to-end through aiohttp via ``aioresponses``.
"""

from __future__ import annotations

import json

from aiohttp import ClientSession
from aioresponses import aioresponses
import pytest

from vizio_smartcast.client import SmartCastClient
from vizio_smartcast.endpoints import EndpointSpec
from vizio_smartcast.errors import (
    VizioAuthError,
    VizioBusyError,
    VizioConnectionError,
    VizioNotFoundError,
    VizioResponseError,
)
from vizio_smartcast.types import AuthRequirement

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


PATH = "/state_extended"
HOST = "example.test"
URL = f"https://{HOST}{PATH}"


def _spec_get(*, auth: AuthRequirement = AuthRequirement.NONE) -> EndpointSpec:
    return EndpointSpec(paths=(PATH,), method="GET", auth=auth, item_cname=None)


def _spec_put(*, auth: AuthRequirement = AuthRequirement.NONE) -> EndpointSpec:
    return EndpointSpec(paths=(PATH,), method="PUT", auth=auth, item_cname=None)


def _spec_get_other_path(path: str = "/x") -> EndpointSpec:
    return EndpointSpec(
        paths=(path,), method="GET", auth=AuthRequirement.NONE, item_cname=None
    )


# ---------------------------------------------------------------------------
# request_raw_json — happy paths
# ---------------------------------------------------------------------------


class TestRequestRawJsonHappy:
    async def test_get_returns_raw_dict(self) -> None:
        with aioresponses() as m:
            m.get(URL, payload={"POWER_STATUS": {"VALUE": 1}})
            client = SmartCastClient(host=HOST)
            try:
                data = await client.request_raw_json(_spec_get())
            finally:
                await client.aclose()
        assert data == {"POWER_STATUS": {"VALUE": 1}}

    async def test_put_sends_body(self) -> None:
        with aioresponses() as m:
            m.put(URL, payload={"OK": True})
            client = SmartCastClient(host=HOST)
            try:
                data = await client.request_raw_json(
                    _spec_put(), body={"hello": "world"}
                )
            finally:
                await client.aclose()
            # Inspect what was actually sent.
            put_call = next(
                req
                for (method, _), reqs in m.requests.items()
                if method == "PUT"
                for req in reqs
            )
            sent = json.loads(put_call.kwargs["data"])
            assert sent == {"hello": "world"}
            assert data == {"OK": True}


# ---------------------------------------------------------------------------
# request_raw_json — error paths
# ---------------------------------------------------------------------------


class TestRequestRawJsonErrors:
    async def test_no_paths_raises(self) -> None:
        spec = EndpointSpec(
            paths=(),
            method="GET",
            auth=AuthRequirement.NONE,
            item_cname=None,
        )
        client = SmartCastClient(host=HOST)
        try:
            with pytest.raises(ValueError, match="no paths"):
                await client.request_raw_json(spec)
        finally:
            await client.aclose()

    async def test_auth_required_no_token_raises(self) -> None:
        spec = _spec_get(auth=AuthRequirement.REQUIRED)
        client = SmartCastClient(host=HOST)  # no auth_token
        try:
            with pytest.raises(VizioAuthError, match="auth token required"):
                await client.request_raw_json(spec)
        finally:
            await client.aclose()

    async def test_http_500_maps_to_connection_error(self) -> None:
        with aioresponses() as m:
            m.get(URL, status=500, body="oops")
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioConnectionError, match="500"):
                    await client.request_raw_json(_spec_get())
            finally:
                await client.aclose()

    async def test_http_403_maps_to_auth_error(self) -> None:
        with aioresponses() as m:
            m.get(URL, status=403, body="")
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioAuthError, match="403"):
                    await client.request_raw_json(_spec_get())
            finally:
                await client.aclose()

    async def test_invalid_json_payload(self) -> None:
        with aioresponses() as m:
            m.get(URL, status=200, body="not json {{{")
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioResponseError, match="parse JSON"):
                    await client.request_raw_json(_spec_get())
            finally:
                await client.aclose()

    async def test_non_object_payload(self) -> None:
        with aioresponses() as m:
            m.get(URL, status=200, body="42")
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioResponseError, match="JSON object"):
                    await client.request_raw_json(_spec_get())
            finally:
                await client.aclose()


# ---------------------------------------------------------------------------
# request_spec — additional status mappings
# ---------------------------------------------------------------------------


def _envelope(*, result: str, detail: str = "", items=None) -> dict:
    body: dict = {"STATUS": {"RESULT": result, "DETAIL": detail}}
    if items is not None:
        body["ITEMS"] = items
    return body


class TestRequestSpecStatusMappings:
    async def test_blocked_status_maps_to_busy(self) -> None:
        spec = _spec_get_other_path("/menu_native/dynamic/foo")
        url = f"https://{HOST}{spec.paths[0]}"
        with aioresponses() as m:
            m.get(url, payload=_envelope(result="BLOCKED", detail="busy"))
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioBusyError, match="busy"):
                    await client.request_spec(spec)
            finally:
                await client.aclose()

    async def test_requires_pairing_maps_to_auth(self) -> None:
        spec = _spec_get_other_path("/menu_native/dynamic/foo")
        url = f"https://{HOST}{spec.paths[0]}"
        with aioresponses() as m:
            m.get(
                url,
                payload=_envelope(result="REQUIRES_PAIRING", detail="needs auth"),
            )
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioAuthError, match="needs auth"):
                    await client.request_spec(spec)
            finally:
                await client.aclose()

    async def test_pairing_denied_maps_to_auth(self) -> None:
        spec = _spec_get_other_path("/menu_native/dynamic/foo")
        url = f"https://{HOST}{spec.paths[0]}"
        with aioresponses() as m:
            m.get(
                url,
                payload=_envelope(result="PAIRING_DENIED", detail="wrong PIN"),
            )
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioAuthError, match="wrong PIN"):
                    await client.request_spec(spec)
            finally:
                await client.aclose()

    async def test_unknown_status_falls_through_to_response_error(self) -> None:
        spec = _spec_get_other_path("/menu_native/dynamic/foo")
        url = f"https://{HOST}{spec.paths[0]}"
        with aioresponses() as m:
            m.get(
                url,
                payload=_envelope(result="FUTURE_VIZIO_CODE", detail="???"),
            )
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioResponseError, match="FUTURE_VIZIO_CODE"):
                    await client.request_spec(spec)
            finally:
                await client.aclose()

    async def test_success_with_missing_required_item_raises_not_found(self) -> None:
        # When a spec has item_cname, SUCCESS without that item triggers
        # NotFound so multi-path fallback can try alternates.
        spec = EndpointSpec(
            paths=("/p",),
            method="GET",
            auth=AuthRequirement.NONE,
            item_cname="esn",
        )
        url = f"https://{HOST}/p"
        with aioresponses() as m:
            m.get(url, payload=_envelope(result="SUCCESS", items=[]))
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioNotFoundError, match="esn"):
                    await client.request_spec(spec)
            finally:
                await client.aclose()

    async def test_invalid_json_in_envelope(self) -> None:
        # request_spec also surfaces JSON parse errors.
        spec = _spec_get_other_path("/p")
        url = f"https://{HOST}/p"
        with aioresponses() as m:
            m.get(url, status=200, body="not json {{{")
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioResponseError):
                    await client.request_spec(spec)
            finally:
                await client.aclose()

    async def test_timeout_maps_to_connection_error(self) -> None:
        spec = _spec_get_other_path("/p")
        url = f"https://{HOST}/p"
        with aioresponses() as m:
            m.get(url, exception=TimeoutError())
            client = SmartCastClient(host=HOST)
            try:
                with pytest.raises(VizioConnectionError):
                    await client.request_spec(spec)
            finally:
                await client.aclose()


# ---------------------------------------------------------------------------
# Auth check + headers
# ---------------------------------------------------------------------------


class TestAuthAndHeaders:
    async def test_request_spec_requires_token_when_required(self) -> None:
        spec = _spec_get_other_path("/p")
        spec_required = EndpointSpec(
            paths=spec.paths,
            method=spec.method,
            auth=AuthRequirement.REQUIRED,
            item_cname=None,
        )
        client = SmartCastClient(host=HOST)
        try:
            with pytest.raises(VizioAuthError, match="auth token required"):
                await client.request_spec(spec_required)
        finally:
            await client.aclose()

    async def test_auth_header_is_sent_when_token_set(self) -> None:
        spec = EndpointSpec(
            paths=("/p",),
            method="GET",
            auth=AuthRequirement.OPTIONAL,
            item_cname=None,
        )
        url = f"https://{HOST}/p"
        with aioresponses() as m:
            m.get(url, payload=_envelope(result="SUCCESS", items=[]))
            client = SmartCastClient(host=HOST, auth_token="MY-TOKEN")
            try:
                await client.request_spec(spec)
            finally:
                await client.aclose()
            req = next(
                r
                for (method, _), reqs in m.requests.items()
                if method == "GET"
                for r in reqs
            )
            headers = req.kwargs.get("headers", {})
            assert headers.get("AUTH") == "MY-TOKEN"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_aclose_idempotent(self) -> None:
        client = SmartCastClient(host=HOST)
        await client.aclose()
        # Second call must not raise.
        await client.aclose()

    async def test_borrowed_session_is_not_closed(self) -> None:
        async with ClientSession() as session:
            client = SmartCastClient(host=HOST, session=session)
            await client.aclose()
            # We did not own the session — it should still be usable.
            assert not session.closed

    async def test_owned_session_is_closed_on_aclose(self) -> None:
        # With session=None, the client lazily creates and owns one.
        with aioresponses() as m:
            m.get(f"https://{HOST}/p", payload=_envelope(result="SUCCESS", items=[]))
            client = SmartCastClient(host=HOST)
            await client.request_spec(_spec_get_other_path("/p"))
            session = client._session  # type: ignore[attr-defined]
            assert session is not None
            await client.aclose()
            assert session.closed


# ---------------------------------------------------------------------------
# Custom timeout coercion
# ---------------------------------------------------------------------------


class TestTimeoutCoercion:
    def test_float_timeout_used(self) -> None:
        client = SmartCastClient(host=HOST, timeout=2.5)
        # Internal timeout's sock_read should reflect the provided value.
        assert client._timeout.sock_read == 2.5  # type: ignore[attr-defined]

    def test_none_timeout_uses_default(self) -> None:
        client = SmartCastClient(host=HOST, timeout=None)
        # The defaults exist and are positive — exact value lives in
        # client.py and may evolve.
        assert client._timeout.sock_read is not None  # type: ignore[attr-defined]
        assert client._timeout.sock_read > 0  # type: ignore[attr-defined]
