"""
API spec version ladder — the gate for firmware-dependent endpoints.

Resolves the ``API_VERSION`` string from ``/state/device/deviceinfo`` to
an ordered :class:`ApiSpec` rung, so callers can gate an endpoint with a
single ``>=`` comparison.

Two version formats are in circulation, compared differently:

- **V2 form** (``3.7.2-2621.0005``) — non-digits stripped, then compared
  **character by character** with the shorter string padded with NUL.
- **Legacy form** (``1.0.13.25``, or prefixed like ``FW_1.0.12.11``) —
  split on ``_``/``-``, keep the trailing component, strip everything but
  digits and dots, compare dot-separated components **numerically**.

Both walk their ladder newest-to-oldest and return the first rung the
target meets or exceeds.

This must resolve identically to the official client, which is why three
things that look like bugs are deliberate. **Do not "fix" them without
changing the gate's meaning:**

- ``_V2_FORM`` is matched with ``fullmatch``, so a prefixed or
  multi-digit version (``FW_3.7.2-2621.0005``, ``3.10.2-2621.0005``)
  falls to the legacy branch and resolves below its true rung.
- ``_resolve_v2`` floors at :attr:`ApiSpec.V2_0_0_2000_0000` even for a
  string below it, so such a version outranks :attr:`ApiSpec.V1_0_13_25`.
- ``_V2_FORM`` escapes its dots, so degenerate separators
  (``3x7x2-2621x0005``) take the legacy branch rather than the V2 one.

Neither of the first two affects :func:`supports_volume_v2`, the only
gate today, which tests the top rung. Revisit if a lower rung ever gates
something.
"""

from __future__ import annotations

from enum import IntEnum
import re

__all__ = ["ApiSpec", "supports_volume_v2"]


_V2_FORM = re.compile(r"\d\.\d\.\d-\d{4}\.\d{4}")

_NON_DIGITS = re.compile(r"\D+")
_NOT_DIGIT_OR_DOT = re.compile(r"[^0-9.]")


class ApiSpec(IntEnum):
    """
    Minimum API spec version a device's firmware satisfies.

    Ordered — compare with ``>=`` to gate a capability.
    """

    V1_0_0_0 = 0
    V1_0_12_11 = 1
    V1_0_13_25 = 2
    V2_0_0_2000_0000 = 3
    V2_0_0_2031_0014 = 4
    """Threshold for the flat ``/audio/volume/*`` endpoint family."""

    @property
    def version_string(self) -> str:
        """The canonical version string this ladder entry represents."""
        return _VERSION_STRINGS[self]

    @classmethod
    def from_version(cls, api_version: str | None) -> ApiSpec:
        """
        Resolve a raw ``API_VERSION`` string to the highest spec it meets.

        Unparseable or missing input resolves to :attr:`V1_0_0_0`, the
        oldest rung — guessing low only costs the legacy code path,
        whereas guessing high offers a device endpoints it may not
        implement.
        """
        if not api_version:
            return cls.V1_0_0_0
        raw = api_version.strip()
        if not raw:
            return cls.V1_0_0_0
        if _V2_FORM.fullmatch(raw):
            return cls._resolve_v2(raw)
        trailing = re.split(r"[_-]", raw)[-1]
        return cls._resolve_legacy(trailing)

    @classmethod
    def _resolve_v2(cls, raw: str) -> ApiSpec:
        """Character-wise compare against the V2 rungs, newest first."""
        for candidate in (cls.V2_0_0_2031_0014, cls.V2_0_0_2000_0000):
            if _compare_digits(raw, candidate.version_string) >= 0:
                return candidate
        return cls.V2_0_0_2000_0000

    @classmethod
    def _resolve_legacy(cls, raw: str) -> ApiSpec:
        """Component-wise numeric compare against the V1 rungs, newest first."""
        sanitized = _NOT_DIGIT_OR_DOT.sub("", raw)
        if not sanitized:
            return cls.V1_0_0_0
        target = sanitized.split(".")
        for candidate in (cls.V1_0_13_25, cls.V1_0_12_11, cls.V1_0_0_0):
            if _at_least(target, candidate.version_string.split(".")):
                return candidate
        return cls.V1_0_0_0


_VERSION_STRINGS: dict[ApiSpec, str] = {
    ApiSpec.V1_0_0_0: "1.0.0.0",
    ApiSpec.V1_0_12_11: "1.0.12.11",
    ApiSpec.V1_0_13_25: "1.0.13.25",
    ApiSpec.V2_0_0_2000_0000: "2.0.0-2000.0000",
    ApiSpec.V2_0_0_2031_0014: "2.0.0-2031.0014",
}


def _compare_digits(version1: str, version2: str) -> int:
    """
    Compare two version strings digit-wise.

    Strips every non-digit from both, then compares character by
    character over ``max(len)`` positions, treating a missing character
    as NUL (so the shorter string sorts first when it is a prefix).
    Returns a negative/zero/positive int like ``cmp``.
    """
    left = _NON_DIGITS.sub("", version1)
    right = _NON_DIGITS.sub("", version2)
    for index in range(max(len(left), len(right))):
        a = ord(left[index]) if index < len(left) else 0
        b = ord(right[index]) if index < len(right) else 0
        if a != b:
            return a - b
    return 0


def _at_least(target: list[str], candidate: list[str]) -> bool:
    """
    Return whether dotted ``target`` meets or exceeds ``candidate``.

    Compares only the components both share; a target with *fewer*
    components than the candidate cannot satisfy it. Non-numeric
    components are skipped rather than failing the whole comparison.
    """
    for left, right in zip(target, candidate, strict=False):
        try:
            mine, theirs = int(left), int(right)
        except ValueError:
            continue
        if mine != theirs:
            return mine > theirs
    return len(target) >= len(candidate)


def supports_volume_v2(api_version: str | None, *, is_tv: bool) -> bool:
    """
    Return whether the device speaks the flat ``/audio/volume/*`` API.

    Requires a TV (settings root other than ``audio_settings``) *and* an
    API version of at least ``2.0.0-2031.0014``.

    Note this is **not** the ``CAPABILITIES."AUDIO_2.0_API"`` flag some
    firmware advertises. That flag exists on the wire but does not
    predict this behavior — a TV can report it and still list ``volume``
    in the ``audio`` collection.
    """
    if not is_tv:
        return False
    return ApiSpec.from_version(api_version) >= ApiSpec.V2_0_0_2031_0014
