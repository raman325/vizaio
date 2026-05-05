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
