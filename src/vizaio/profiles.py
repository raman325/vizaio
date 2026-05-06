"""
Built-in :class:`DeviceProfile` presets for known Vizio device families.

Users can:
- Use a preset: ``Vizio(host, profile=DeviceType.TV.profile)``
- Use the enum shortcut: ``Vizio(host, device_type=DeviceType.TV)``
- Construct custom: ``Vizio(host, profile=DeviceProfile(name="custom", ...))``

Crave variants identified per APK findings: the Vizio mobile app
distinguishes Go (SP30-E0), 360 (SP50-D5), and Pro (SP70-D5) by model
string. All share the audio_settings tree and battery support; max
volumes differ per hardware.
"""

from __future__ import annotations

from ._keys import CRAVE360_KEYS, SOUNDBAR_KEYS, TV_KEYS
from .endpoints import SettingsRoot
from .types import DeviceProfile, DeviceType

TV_PROFILE = DeviceProfile(
    name="Vizio SmartCast TV",
    settings_root=SettingsRoot.TV,
    max_volume=100,
    requires_auth=True,
    has_battery=False,
    has_inputs=True,
    has_apps=True,
    keymap=TV_KEYS,
)

SOUNDBAR_PROFILE = DeviceProfile(
    name="Vizio SmartCast Soundbar",
    settings_root=SettingsRoot.AUDIO,
    # Soundbars use a 0-31 scale — see protocol-notes #10 and pyvizio
    # issue #125 (user setting volume=1 maxed out a soundbar).
    max_volume=31,
    requires_auth=False,
    has_battery=False,
    has_inputs=False,
    has_apps=False,
    keymap=SOUNDBAR_KEYS,
)


# Crave variants share keymap, settings_root, and (currently) volume scale.
# Hardware testing may reveal per-model max_volume divergence; if so, the
# table below is where the difference becomes visible.
def _crave(name: str, max_volume: int = 24) -> DeviceProfile:
    return DeviceProfile(
        name=name,
        settings_root=SettingsRoot.AUDIO,
        max_volume=max_volume,
        requires_auth=False,
        has_battery=True,
        has_inputs=False,
        has_apps=False,
        keymap=CRAVE360_KEYS,
    )


CRAVE_GO_PROFILE = _crave("Vizio Crave Go (SP30-E0)")
CRAVE360_PROFILE = _crave("Vizio Crave 360 (SP50-D5)")
CRAVE_PRO_PROFILE = _crave("Vizio Crave Pro (SP70-D5)")


PROFILES: dict[DeviceType, DeviceProfile] = {
    DeviceType.TV: TV_PROFILE,
    DeviceType.SOUNDBAR: SOUNDBAR_PROFILE,
    DeviceType.CRAVE_GO: CRAVE_GO_PROFILE,
    DeviceType.CRAVE360: CRAVE360_PROFILE,
    DeviceType.CRAVE_PRO: CRAVE_PRO_PROFILE,
}
