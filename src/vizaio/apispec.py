"""
API spec version ladder — the gate for firmware-dependent endpoints.

Vizio ships one mobile app against many firmware generations, so the app
itself must decide at runtime which protocol surface a device speaks. It
does that from the ``API_VERSION`` string in the unauthenticated
``/state/device/deviceinfo`` payload, mapped onto an ordered ladder of
*minimum spec versions*. This module is a direct port of that logic, so
``vizaio`` reaches the same conclusion the official client would.

Source: ``com/vizio/vnf/network/message/device/DeviceInfoAnalyzer.java``
in the ``com.vizio.vue.launcher`` 5.0.0 decompile (``RestApiUtil``, plus
the ``ApiMinimumSpecVersion`` enum). See ``docs/android-app-findings.md``.

Two string shapes exist, and the app treats them with two *different*
comparison algorithms:

- **V2 form** — ``3.7.2-2621.0005``. All non-digits are stripped and the
  remainder is compared **character by character**, shorter string padded
  with NUL. Since every real V2 string sanitizes to 11 digits this
  behaves like a numeric compare, but the character semantics are
  preserved here so odd-length strings order the way the device's client
  would order them.
- **Legacy form** — ``1.0.13.25``, or prefixed like ``FW_1.0.12.11``. The
  app splits on ``_``/``-``, keeps the trailing component, strips
  everything but digits and dots, then compares dot-separated components
  **numerically**.

Both branches walk their candidate ladder from newest to oldest and
return the first entry the target meets or exceeds.
"""

from __future__ import annotations

from enum import IntEnum
import re

__all__ = ["ApiSpec", "supports_volume_v2"]


# The app's literal pattern is ``\d.\d.\d-\d{4}.\d{4}`` — with *unescaped*
# dots, so it also matches separators like ``3x7x2-2621x0005``. The dots
# are escaped here: no real device emits the degenerate forms, and a
# stricter pattern keeps the branch choice predictable.
_V2_FORM = re.compile(r"\d\.\d\.\d-\d{4}\.\d{4}")

_NON_DIGITS = re.compile(r"\D+")
_NOT_DIGIT_OR_DOT = re.compile(r"[^0-9.]")


class ApiSpec(IntEnum):
    """
    Minimum API spec version a device's firmware satisfies.

    Ordered — compare with ``>=`` to gate a capability. Values mirror
    the ``id`` field of the app's ``ApiMinimumSpecVersion`` enum so the
    ordering is identical.
    """

    V1_0_0_0 = 0
    V1_0_12_11 = 1
    V1_0_13_25 = 2
    V2_0_0_2000_0000 = 3
    V2_0_0_2031_0014 = 4
    """Threshold for the flat ``/audio/volume/*`` endpoint family. Named
    ``VER_2_0_0_2031_0014_FUR_SUPPORTED`` in the app."""

    @property
    def version_string(self) -> str:
        """The canonical version string this ladder entry represents."""
        return _VERSION_STRINGS[self]

    @classmethod
    def from_version(cls, api_version: str | None) -> ApiSpec:
        """
        Resolve a raw ``API_VERSION`` string to the highest spec it meets.

        Unparseable or missing input resolves to :attr:`V1_0_0_0` — the
        oldest rung. Degrading downward is the safe direction: it only
        costs the legacy code path, whereas guessing high would offer a
        device endpoints its firmware does not implement.
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
    Compare two version strings the way the app's ``compareVersion`` does.

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
    components than the candidate cannot satisfy it (the app breaks out
    of the loop in that case). Non-numeric components are skipped rather
    than failing the whole comparison.
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

    Mirrors ``DeviceInfoAnalyzer.isVolumeAPIV2Supported()``: the device
    must be a TV (settings root other than ``audio_settings``) *and*
    report an API version of at least ``2.0.0-2031.0014``.

    Note this is **not** the ``CAPABILITIES."AUDIO_2.0_API"`` flag some
    firmware advertises. That flag is real on the wire but the official
    app never reads it, and it does not predict this behavior — a TV can
    report it while still listing ``volume`` in the ``audio`` collection.
    """
    if not is_tv:
        return False
    return ApiSpec.from_version(api_version) >= ApiSpec.V2_0_0_2031_0014
