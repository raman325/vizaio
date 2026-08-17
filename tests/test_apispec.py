"""API spec version parsing and the volume-V2 capability threshold.

Vizio firmware advertises an ``API_VERSION`` string in the
unauthenticated ``deviceinfo`` payload. The official Android app maps
that string onto an ordered ladder of *minimum spec versions* and gates
its newer flat volume endpoints on clearing a threshold — see
``DeviceInfoAnalyzer.isVolumeAPIV2Supported()`` in the 5.0.0 decompile
and ``docs/android-app-findings.md``.

Two string shapes exist in the wild:

- **V2 form** ``3.7.2-2621.0005`` — matches ``\\d.\\d.\\d-\\d{4}.\\d{4}``
  and is compared component-wise against the ladder entries.
- **Legacy form** ``1.0.13.25`` / ``FW_1.0.12.11`` — the app splits on
  ``_``/``-`` and compares only the trailing component.

These tests pin the comparison semantics so the capability check can't
silently drift.
"""

from __future__ import annotations

import pytest

from vizaio.apispec import ApiSpec, supports_volume_v2


class TestApiSpecOrdering:
    """The ladder itself: which spec version a raw string resolves to."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # V2-form strings from real devices.
            ("3.7.2-2621.0005", ApiSpec.V2_0_0_2031_0014),  # issue #179254 TV
            ("3.3.3-2538.0001", ApiSpec.V2_0_0_2031_0014),  # captured VHD24M-0810
            ("2.0.0-2031.0014", ApiSpec.V2_0_0_2031_0014),  # exactly the threshold
            ("2.0.0-2031.0013", ApiSpec.V2_0_0_2000_0000),  # one below it
            ("2.0.0-2000.0000", ApiSpec.V2_0_0_2000_0000),
            # Legacy dotted forms compare on the trailing component.
            ("1.0.13.25", ApiSpec.V1_0_13_25),
            ("1.0.12.11", ApiSpec.V1_0_12_11),
            ("1.0.0.0", ApiSpec.V1_0_0_0),
        ],
    )
    def test_resolves_known_versions(self, raw: str, expected: ApiSpec) -> None:
        assert ApiSpec.from_version(raw) is expected

    @pytest.mark.parametrize("raw", ["", "   ", "not-a-version", "junk"])
    def test_unparseable_falls_back_to_oldest(self, raw: str) -> None:
        """Unknown strings degrade to the oldest spec, never to the newest.

        Guessing high would hand a legacy device endpoints it does not
        implement; guessing low only costs us the older code path.
        """
        assert ApiSpec.from_version(raw) is ApiSpec.V1_0_0_0

    def test_ladder_is_ordered(self) -> None:
        assert ApiSpec.V1_0_0_0 < ApiSpec.V1_0_12_11 < ApiSpec.V1_0_13_25
        assert ApiSpec.V1_0_13_25 < ApiSpec.V2_0_0_2000_0000
        assert ApiSpec.V2_0_0_2000_0000 < ApiSpec.V2_0_0_2031_0014


class TestVolumeV2Capability:
    """``supports_volume_v2`` — the app's gate, reproduced."""

    @pytest.mark.parametrize(
        "raw", ["3.7.2-2621.0005", "3.3.3-2538.0001", "2.0.0-2031.0014"]
    )
    def test_modern_tv_supports_v2(self, raw: str) -> None:
        assert supports_volume_v2(raw, is_tv=True) is True

    @pytest.mark.parametrize("raw", ["2.0.0-2031.0013", "1.0.13.25", ""])
    def test_older_tv_does_not(self, raw: str) -> None:
        assert supports_volume_v2(raw, is_tv=False) is False

    def test_audio_devices_never_support_v2(self) -> None:
        """``isTvDevice()`` gates the check — soundbars and Crave are out
        even on firmware whose API version clears the threshold."""
        assert supports_volume_v2("3.7.2-2621.0005", is_tv=False) is False
