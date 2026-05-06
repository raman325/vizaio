"""Test fixtures.

TODO: populate during implementation phase. Planned fixtures:

- ``mock_aiohttp``: aioresponses context manager with helpers for SmartCast
  response envelopes (``make_item_response``, ``make_settings_response``, ...).
- ``vizio_tv``: ``Vizio`` instance configured for a fake TV at a known host.
- ``vizio_soundbar``: same, for soundbar device type.
- ``tmp_config``: tmp-path-backed ``Config`` for CLI tests.
- ``cli_runner``: ``typer.testing.CliRunner`` with ``--config`` pointed at
  ``tmp_config``.
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
