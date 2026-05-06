"""
SmartCastClient transport-layer behavior — end-to-end against mocked HTTP.

The unit-level wire/parse tests cover envelope parsing in isolation. These
tests cover what happens when those parses meet the multi-path
:class:`EndpointSpec` and the device-side error-status zoo.
"""

from __future__ import annotations

import json

from aioresponses import aioresponses
import pytest

from vizaio.client import SmartCastClient
from vizaio.endpoints import EndpointSpec
from vizaio.errors import (
    VizioConnectionError,
    VizioNotFoundError,
    VizioResponseError,
)
from vizaio.types import AuthRequirement


def _envelope(
    *, result: str, detail: str = "", items: list[dict] | None = None
) -> dict:
    body: dict = {"STATUS": {"RESULT": result, "DETAIL": detail}}
    if items is not None:
        body["ITEMS"] = items
    return body


class TestEndpointFallbackOnUriNotFound:
    """
    Captured live from VHD24M-0810 firmware 3.720.9.1-1: requesting
    ``/menu_native/dynamic/tv_settings/admin_and_privacy/system_information/tv_information/esn``
    returns ``HTTP 200`` with ``STATUS.RESULT="URI_NOT_FOUND"``. Older
    firmware exposes the data at a different path.

    The transport must:
    1. Recognize ``URI_NOT_FOUND`` (not a synonym for "any device error")
       and turn it into :class:`VizioNotFoundError`.
    2. Fall through to the next candidate path, surfacing the alt path's
       response when it succeeds.
    """

    async def test_falls_through_to_alt_path_on_uri_not_found(self) -> None:
        primary = "/menu_native/dynamic/tv_settings/admin_and_privacy/foo/esn"
        fallback = "/menu_native/dynamic/tv_settings/system/foo/esn"
        spec = EndpointSpec(
            paths=(primary, fallback),
            method="GET",
            auth=AuthRequirement.NONE,
            item_cname="esn",
        )

        with aioresponses() as m:
            m.get(
                f"https://example.test{primary}",
                status=200,
                body=json.dumps(
                    _envelope(result="URI_NOT_FOUND", detail="URI not found")
                ),
            )
            m.get(
                f"https://example.test{fallback}",
                status=200,
                body=json.dumps(
                    _envelope(
                        result="SUCCESS",
                        items=[
                            {
                                "CNAME": "esn",
                                "TYPE": "T_STRING_V1",
                                "NAME": "ESN",
                                "VALUE": "FALLBACK-ESN",
                                "HASHVAL": 1,
                            }
                        ],
                    )
                ),
            )
            client = SmartCastClient(host="example.test")
            try:
                response = await client.request_spec(spec)
            finally:
                await client.aclose()

        assert response.has_item("esn")
        assert response.find_item("esn").value == "FALLBACK-ESN"  # type: ignore[union-attr]

    async def test_uri_not_found_propagates_when_no_fallback_path(self) -> None:
        """
        Single-path endpoint with URI_NOT_FOUND should raise
        :class:`VizioNotFoundError`, not the previous behavior of
        :class:`VizioResponseError` for unknown statuses.
        """
        path = "/menu_native/dynamic/tv_settings/some/leaf"
        spec = EndpointSpec(
            paths=(path,),
            method="GET",
            auth=AuthRequirement.NONE,
            item_cname=None,
        )

        with aioresponses() as m:
            m.get(
                f"https://example.test{path}",
                status=200,
                body=json.dumps(
                    _envelope(result="URI_NOT_FOUND", detail="URI not found")
                ),
            )
            client = SmartCastClient(host="example.test")
            try:
                with pytest.raises(VizioNotFoundError, match="URI not found"):
                    await client.request_spec(spec)
            finally:
                await client.aclose()

    async def test_http_403_maps_to_vizio_auth_error(self) -> None:
        """
        Captured live from VHD24M-0810 fw 3.720.9.1-1: re-pairing with
        the same device_id invalidates the previous token. Subsequent
        calls with the old token return raw HTTP 403 (not the SCPL
        envelope shape with PAIRING_DENIED). Library must surface this
        as ``VizioAuthError`` so callers can distinguish "token invalid,
        re-pair needed" from "device unreachable / network problem."
        """
        from vizaio.errors import VizioAuthError

        path = "/state/device/power_mode"
        spec = EndpointSpec(
            paths=(path,),
            method="GET",
            auth=AuthRequirement.OPTIONAL,
            item_cname=None,
        )
        with aioresponses() as m:
            m.get(f"https://example.test{path}", status=403, body="")
            client = SmartCastClient(host="example.test", auth_token="bad")
            try:
                with pytest.raises(VizioAuthError, match="HTTP 403"):
                    await client.request_spec(spec)
            finally:
                await client.aclose()

    async def test_http_500_still_maps_to_connection_error(self) -> None:
        """Non-auth HTTP errors continue to raise VizioConnectionError —
        the 401/403 mapping must not over-broaden to all non-200s."""
        path = "/state/device/whatever"
        spec = EndpointSpec(
            paths=(path,),
            method="GET",
            auth=AuthRequirement.NONE,
            item_cname=None,
        )
        with aioresponses() as m:
            m.get(f"https://example.test{path}", status=500, body="")
            client = SmartCastClient(host="example.test")
            try:
                with pytest.raises(VizioConnectionError, match="HTTP 500"):
                    await client.request_spec(spec)
            finally:
                await client.aclose()

    async def test_unknown_status_still_raises_response_error(self) -> None:
        """
        Regression: only URI_NOT_FOUND should be treated as not-found.
        A status string we don't recognize must still raise
        :class:`VizioResponseError` so the user sees the device's
        actual response and we don't hide bugs by being too permissive.
        """
        path = "/state/device/whatever"
        spec = EndpointSpec(
            paths=(path,),
            method="GET",
            auth=AuthRequirement.NONE,
            item_cname=None,
        )

        with aioresponses() as m:
            m.get(
                f"https://example.test{path}",
                status=200,
                body=json.dumps(_envelope(result="SOME_FUTURE_STATUS", detail="??")),
            )
            client = SmartCastClient(host="example.test")
            try:
                with pytest.raises(VizioResponseError, match="SOME_FUTURE_STATUS"):
                    await client.request_spec(spec)
            finally:
                await client.aclose()
