"""DeviceProfile presets and capability gating.

Profiles are the unit of capability variation. These tests lock in:

- Built-in presets exist for the three known device families
- Capability flags match observed device behavior (e.g., soundbars don't
  require auth; only Crave 360 has a battery)
- Volume scales match what each device family actually reports
- Keymaps include the keys we expect for each family
- Custom profiles work end-to-end (no hidden coupling to the presets)

When the agent (#34) and APK decompilation (#35) finish, we may discover
additional capabilities to expose. Add them here first.
"""

from __future__ import annotations

import pytest

from vizaio import (
    CRAVE360_PROFILE,
    CRAVE_GO_PROFILE,
    CRAVE_PRO_PROFILE,
    SOUNDBAR_PROFILE,
    TV_PROFILE,
    DeviceProfile,
    DeviceType,
    RemoteKey,
)
from vizaio.endpoints import SettingsRoot

PROFILES_AND_TYPES = [
    (TV_PROFILE, DeviceType.TV),
    (SOUNDBAR_PROFILE, DeviceType.SOUNDBAR),
    (CRAVE_GO_PROFILE, DeviceType.CRAVE_GO),
    (CRAVE360_PROFILE, DeviceType.CRAVE360),
    (CRAVE_PRO_PROFILE, DeviceType.CRAVE_PRO),
]

CRAVE_PROFILES = [CRAVE_GO_PROFILE, CRAVE360_PROFILE, CRAVE_PRO_PROFILE]


class TestPresetsExist:
    """All three built-in profiles are reachable via DeviceType."""

    @pytest.mark.parametrize("profile,dtype", PROFILES_AND_TYPES)
    def test_device_type_resolves_to_profile(
        self, profile: DeviceProfile, dtype: DeviceType
    ) -> None:
        assert dtype.profile is profile

    def test_profiles_are_distinct_objects(self) -> None:
        assert TV_PROFILE is not SOUNDBAR_PROFILE
        assert SOUNDBAR_PROFILE is not CRAVE360_PROFILE
        assert CRAVE_GO_PROFILE is not CRAVE360_PROFILE
        assert CRAVE360_PROFILE is not CRAVE_PRO_PROFILE


class TestVolumeScales:
    """Per-protocol-notes #10: TVs use 0-100, soundbars 0-31, Crave 360 0-24."""

    def test_tv_max_volume(self) -> None:
        assert TV_PROFILE.max_volume == 100

    def test_soundbar_max_volume(self) -> None:
        # Vizio sound bars use 0-31 — see protocol-notes #10 and pyvizio
        # issue #125 (user confusion: setting volume=1 maxes a soundbar).
        assert SOUNDBAR_PROFILE.max_volume == 31

    @pytest.mark.parametrize("profile", CRAVE_PROFILES)
    def test_crave_max_volume(self, profile: DeviceProfile) -> None:
        # All Crave variants share the 0-24 scale per pyvizio. Hardware
        # smoke test (#29) is the source of truth — update if Go or Pro
        # turn out to differ.
        assert profile.max_volume == 24


class TestAuthRequirements:
    """Soundbars and Crave 360 don't require auth; TVs do."""

    def test_tv_requires_auth(self) -> None:
        assert TV_PROFILE.requires_auth is True

    def test_soundbar_no_auth(self) -> None:
        assert SOUNDBAR_PROFILE.requires_auth is False

    @pytest.mark.parametrize("profile", CRAVE_PROFILES)
    def test_crave_no_auth(self, profile: DeviceProfile) -> None:
        assert profile.requires_auth is False


class TestCapabilityFlags:
    """Battery, inputs, apps gates."""

    def test_only_crave_has_battery(self) -> None:
        for profile in CRAVE_PROFILES:
            assert profile.has_battery is True, f"{profile.name} should have battery"
        assert TV_PROFILE.has_battery is False
        assert SOUNDBAR_PROFILE.has_battery is False

    def test_only_tv_has_inputs(self) -> None:
        # Soundbars and Crave devices don't expose individual inputs
        # through this API.
        assert TV_PROFILE.has_inputs is True
        assert SOUNDBAR_PROFILE.has_inputs is False
        for profile in CRAVE_PROFILES:
            assert profile.has_inputs is False

    def test_only_tv_has_apps(self) -> None:
        # SmartCast apps live on TVs, not on soundbars/Crave.
        assert TV_PROFILE.has_apps is True
        assert SOUNDBAR_PROFILE.has_apps is False
        for profile in CRAVE_PROFILES:
            assert profile.has_apps is False


class TestSettingsRoot:
    """TVs use tv_settings; soundbars/Crave use audio_settings."""

    def test_tv_settings_root(self) -> None:
        assert TV_PROFILE.settings_root is SettingsRoot.TV

    def test_soundbar_settings_root(self) -> None:
        assert SOUNDBAR_PROFILE.settings_root is SettingsRoot.AUDIO

    @pytest.mark.parametrize("profile", CRAVE_PROFILES)
    def test_crave_settings_root(self, profile: DeviceProfile) -> None:
        assert profile.settings_root is SettingsRoot.AUDIO


class TestKeymaps:
    """Keymap membership reflects what each family physically can do."""

    @pytest.mark.parametrize(
        "profile",
        [TV_PROFILE, SOUNDBAR_PROFILE, *CRAVE_PROFILES],
    )
    def test_universal_keys_present(self, profile: DeviceProfile) -> None:
        # Power and volume work everywhere.
        for k in (
            RemoteKey.POW_ON,
            RemoteKey.POW_OFF,
            RemoteKey.POW_TOGGLE,
            RemoteKey.VOL_UP,
            RemoteKey.VOL_DOWN,
            RemoteKey.MUTE_ON,
            RemoteKey.MUTE_OFF,
        ):
            assert k in profile.keymap, f"{k} missing from {profile.name}"

    def test_tv_only_keys(self) -> None:
        # Channel and TV navigation keys exist only on TVs.
        for k in (RemoteKey.CH_UP, RemoteKey.CH_DOWN, RemoteKey.CH_PREV):
            assert k in TV_PROFILE.keymap
            assert k not in SOUNDBAR_PROFILE.keymap
            for profile in CRAVE_PROFILES:
                assert k not in profile.keymap

    def test_keymap_values_are_codeset_code_pairs(self) -> None:
        for code in TV_PROFILE.keymap.values():
            assert isinstance(code, tuple)
            assert len(code) == 2
            assert all(isinstance(n, int) for n in code)


class TestCustomProfile:
    """Library users can build a DeviceProfile for unusual or future devices.

    This is the contract that makes capability profiles valuable: the
    library doesn't need a release for every new Vizio device class.
    """

    def test_construct_custom_profile(self) -> None:
        custom = DeviceProfile(
            name="Hypothetical Vizio Cinema",
            settings_root=SettingsRoot.AUDIO,
            max_volume=50,
            requires_auth=True,
            has_battery=False,
            has_inputs=True,
            has_apps=True,
            keymap={
                RemoteKey.POW_ON: (11, 1),
                RemoteKey.POW_OFF: (11, 0),
                RemoteKey.VOL_UP: (5, 1),
                RemoteKey.VOL_DOWN: (5, 0),
            },
        )
        assert custom.name == "Hypothetical Vizio Cinema"
        assert custom.max_volume == 50
        assert RemoteKey.POW_ON in custom.keymap

    def test_custom_profile_keymap_is_sealed(self) -> None:
        """frozen=True on DeviceProfile prevents accidental mutation that
        would surprise other instances sharing the same dict reference."""
        with pytest.raises((AttributeError, TypeError)):
            TV_PROFILE.max_volume = 999  # type: ignore[misc]
