"""Shared pytest fixtures for the vizaio test suite.

Most fixtures live alongside the tests that use them; this file holds
fixtures shared across multiple test files.
"""

from __future__ import annotations

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
