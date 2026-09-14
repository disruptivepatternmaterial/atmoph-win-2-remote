"""What a Window 2 exposes, and what each part of it means.

Everything here is recovered knowledge rather than logic: the UUID labels from
docs/PROTOCOL.md, the quick-settings keys the report reasons about, the limits
the report renders values within, and the prose behind each LED verdict. It is
separate from the code that reads it because it changes when a window is
observed doing something new, not when the tool changes.
"""

from __future__ import annotations

from atmoph_wire import (
    COMMAND_UUID,
    FOCUSING_VIEW_UUID,
    IDENTITY_UUID,
    PANORAMA_ROLE_UUID,
    POWER_UUID,
    QUICK_SETTINGS_UUID,
    SERVICE_UUID,
    TEXT_INPUT_UUID,
    VIEW_ID_UUID,
    VIEW_IMAGE_UUID,
    VIEW_LOCATION_UUID,
    VIEW_TITLE_UUID,
)

# Absent from the app bytecode and reported on hardware. Capturing it is one of
# the reasons this tool exists; see docs/PROTOCOL.md.
SECOND_SERVICE_UUID = "401f7f45-2258-4f9b-8204-f8b301b4dcc5"

LED_KEY = "LedBrightness"
SOUND_ONLY_KEY = "SoundOnly"
DECORATION_KEY = "CurrentDecoration"
SCREEN_BRIGHTNESS_KEY = "ScreenBrightness"

# The app requests this and gets a 125-byte ATT payload. bleak exposes no MTU
# request API: both CoreBluetooth and BlueZ negotiate it for you, so the dump
# records what was negotiated rather than what was asked for.
REQUESTED_MTU = 128

MAX_VALUE_BYTES = 512
HEX_THRESHOLD = 64
MASK = "<masked>"

# Labels recovered from docs/PROTOCOL.md. "app" marks a characteristic the
# Android app actually binds; "declared" marks one it names and never touches;
# "hardware" marks one reported on a window and absent from the app entirely.
# Deliberately a superset of the protocol layer's constants: every UUID
# protocol.py exports has to appear here, and the hardware-only ones have no
# constant to name them. tests/tools/test_diag.py asserts that direction.
KNOWN_CHARACTERISTICS: dict[str, str] = {
    IDENTITY_UUID: "app: device UUID and name",
    PANORAMA_ROLE_UUID: "app: panorama role",
    VIEW_TITLE_UUID: "app: current view title",
    VIEW_IMAGE_UUID: "app: current view image URL",
    VIEW_LOCATION_UUID: "app: current view location",
    FOCUSING_VIEW_UUID: "app: focused UI element JSON",
    COMMAND_UUID: "app: remote control commands",
    TEXT_INPUT_UUID: "app: text input JSON",
    QUICK_SETTINGS_UUID: "app: quick settings JSON",
    POWER_UUID: "app: display power",
    VIEW_ID_UUID: "declared: view id and revision",
    "e6f3269f-a0ce-49fa-9c46-8edbc02e0711": "declared: device UUID alone",
    "2e109d28-1008-4cb6-a7af-1fabb2fa3278": "declared: device name alone",
    "d78f7085-8a3e-487e-8691-9b672aeea0eb": "declared: write-only, unknown",
    "bef2f796-7d49-48e1-9da6-f24346e6aaf7": "declared: built and discarded",
    "e9c45eb5-fa81-4760-9b1b-24d6cb1d562c": "declared: view id, writes ignored",
    "ac0c2536-1713-4be5-97e6-2c281ebb2544": "declared: view id plus a flag",
    "596f4372-1456-4038-8bca-19ef89e6fe3e": "hardware: child lock JSON",
    "750b35af-a702-4407-95a9-5af779a61785": "declared: token or content hash",
    "e0b4d938-0dec-4e84-ace6-4d81fe4007b6": "hardware: empty JSON object",
    "d39a8ae0-a159-4efd-8ee4-c10c698f5fe2": "hardware: panorama-role shaped",
    "492783d7-d81b-4e54-b7d4-04ca1bcf34f7": "declared: unknown",
    "822962c8-d99c-4f3f-9680-0ed8fc312d4b": "declared: unknown",
    "b95dc23d-0b6b-4b6f-9b8a-3a1a3fa1f2ac": "hardware: unknown",
    "18046ba0-6ba0-4f6e-9b6b-1e0a4d8bd7fd": "hardware: unknown",
    "2330f10b-d28c-4b0e-89c7-8dbd05dfa491": "declared: write-only, unknown",
    "b5b8a6c1-79fb-4220-a5b7-90bbc86a732d": "hardware: three-field identity",
    "00002a00-0000-1000-8000-00805f9b34fb": "GAP: device name",
    "00002aa6-0000-1000-8000-00805f9b34fb": "GAP: central address resolution",
}

# Characteristics whose value is a per-unit identifier. --normalize masks these
# so one dump can be diffed against another unit's, and shared, without
# leaking device identity.
IDENTITY_CHARACTERISTICS = frozenset(
    {
        IDENTITY_UUID,
        "e6f3269f-a0ce-49fa-9c46-8edbc02e0711",
        "2e109d28-1008-4cb6-a7af-1fabb2fa3278",
        "b5b8a6c1-79fb-4220-a5b7-90bbc86a732d",
        "00002a00-0000-1000-8000-00805f9b34fb",
        "750b35af-a702-4407-95a9-5af779a61785",
    }
)

SERVICE_LABELS = {
    SERVICE_UUID: "vendor service the app uses",
    SECOND_SERVICE_UUID: "second vendor service, unknown to the app",
    "00001800-0000-1000-8000-00805f9b34fb": "Generic Access",
    "00001801-0000-1000-8000-00805f9b34fb": "Generic Attribute",
}

LED_VERDICTS = {
    "no-document": (
        "No quick-settings document was obtained, so the LED question is "
        "unanswered. The characteristic may not be readable on this firmware; "
        "re-run with --provoke so the window announces its state."
    ),
    "key-absent": (
        f"{LED_KEY} is absent from a populated document. This firmware build "
        "does not model LEDs on this unit at all. If a working unit reports "
        "the key, the difference is in firmware or model configuration rather "
        "than in the setting value, and no write will ever help."
    ),
    "zero-max": (
        f"{LED_KEY} is present but reports max 0, so the firmware knows the "
        "setting and believes there is no usable LED range. Treat this as the "
        "firmware saying the unit has no LED hardware, or has it disabled "
        "below the settings layer."
    ),
    "malformed": (
        f"{LED_KEY} is present but is not a min/max/value object, so it cannot "
        "be interpreted. Capture the raw document and compare it against a "
        "working unit."
    ),
    "range-off": (
        f"{LED_KEY} reports a usable range with value 0, which means the LEDs "
        "are simply switched off. Write a non-zero value with --led-write and "
        "watch for the echo."
    ),
    "range-on": (
        f"{LED_KEY} reports a usable range with a non-zero value, so the "
        "firmware believes the LEDs are lit. If they are physically dark the "
        "fault is below the settings layer: LED hardware, its cable, or a gate "
        "this protocol does not expose."
    ),
}
