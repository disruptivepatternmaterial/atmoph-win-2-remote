"""Atmoph Window 2 BLE protocol primitives.

This module has no Home Assistant imports so the wire protocol can be tested
and reused independently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Final

SERVICE_UUID: Final = "c1e0d952-12f7-4c84-b67d-fc26f55243a0"
IDENTITY_UUID: Final = "5a388825-de5b-45bf-8864-16be820fc169"
PANORAMA_ROLE_UUID: Final = "ec812b51-ae67-4cf3-8272-3967b3fc22a0"
VIEW_TITLE_UUID: Final = "1d862803-b301-4548-bece-1f1ab61881b8"
VIEW_IMAGE_UUID: Final = "99cd2547-0640-485c-9996-e0a2b384a6f2"
VIEW_LOCATION_UUID: Final = "275ddae2-4c69-4638-97d4-d5ba8e9e05d1"
FOCUSING_VIEW_UUID: Final = "3afad096-8cb5-4cb7-a8b4-c7dca3e41b94"
COMMAND_UUID: Final = "d4393824-471f-4799-ab74-28879878a4e7"
TEXT_INPUT_UUID: Final = "73de7799-b573-46f3-99fd-c6a7a8fc2fde"
QUICK_SETTINGS_UUID: Final = "530bcd10-f723-4203-8222-0e135022d394"
POWER_UUID: Final = "7607f5a4-22bc-4730-9019-c78dc8b50341"

COMMANDS: Final[dict[str, bytes]] = {
    "tap": b"T",
    "double_tap": b"DT",
    "up": b"U",
    "down": b"D",
    "left": b"L",
    "right": b"R",
    "next_view": b"FW",
    "previous_view": b"BW",
    "menu": b"M",
    "quick_menu": b"Q",
    "back": b"B",
    "views": b"V",
    "search": b"VS",
    "sleep_toggle": b"S",
    "connect_notify": b"C",
}

SETTING_KEYS: Final = frozenset(
    {
        "WidgetsVisible",
        "DailyRoutineEnable",
        "LandscapeVolumeLevel",
        "SoundscapeLayer",
        "SoundscapeVolumeLevel",
        "ScreenBrightness",
        "CurrentDecoration",
        "SoundOnly",
        "LedBrightness",
    }
)


@dataclass(slots=True)
class Level:
    """A bounded numeric value reported by the window."""

    minimum: int
    maximum: int
    value: int

    @classmethod
    def from_wire(cls, value: object) -> Level | None:
        """Parse a quick-setting level object."""
        if not isinstance(value, dict):
            return None
        try:
            return cls(
                minimum=int(value["min"]),
                maximum=int(value["max"]),
                value=int(value["value"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(slots=True)
class AtmophState:
    """Latest values reported by an Atmoph Window."""

    device_uuid: str | None = None
    name: str | None = None
    panorama_role: str | None = None
    view_title: str | None = None
    view_image_url: str | None = None
    view_location: str | None = None
    power: bool | None = None
    quick_settings: dict[str, object] = field(default_factory=dict)

    def apply_identity(self, payload: bytes) -> None:
        """Parse the app's comma-separated device UUID and display name."""
        parts = decode_text(payload).split(",", 1)
        self.device_uuid = parts[0] or None
        self.name = parts[1] if len(parts) > 1 and parts[1] else None

    def apply_power(self, payload: bytes) -> None:
        """Parse a boolean power notification."""
        value = decode_text(payload).lower()
        if value not in {"true", "false"}:
            raise ValueError(f"Unexpected power payload: {value!r}")
        self.power = value == "true"

    def apply_quick_settings(self, payload: dict[str, object]) -> None:
        """Merge a complete quick-settings document."""
        self.quick_settings.update(payload)

    def apply_setting_write(self, name: str, value: bool | int | str) -> None:
        """Record a value written locally until the window reports it back.

        A level is reported as an object carrying the bounds alongside the
        value, but written as the bare value. Storing what was written would
        drop the bounds and leave the setting looking unreported.
        """
        current = self.quick_settings.get(name)
        if isinstance(current, dict) and "value" in current:
            self.quick_settings[name] = {**current, "value": value}
        else:
            self.quick_settings[name] = value


class JsonObjectStream:
    """Reassemble one or more UTF-8 JSON objects split across BLE packets."""

    def __init__(self, max_size: int = 16_384) -> None:
        self._buffer = ""
        self._decoder = json.JSONDecoder()
        self._max_size = max_size

    def feed(self, payload: bytes) -> list[dict[str, object]]:
        """Append bytes and return every complete JSON object."""
        self._buffer += decode_text(payload)
        if len(self._buffer) > self._max_size:
            self._buffer = ""
            raise ValueError("JSON notification buffer exceeded its size limit")

        results: list[dict[str, object]] = []
        while self._buffer:
            stripped = self._buffer.lstrip()
            try:
                value, end = self._decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                if not stripped.startswith("{"):
                    start = stripped.find("{")
                    self._buffer = stripped[start:] if start >= 0 else ""
                break
            self._buffer = stripped[end:]
            if isinstance(value, dict):
                results.append(value)
        return results


def decode_text(payload: bytes) -> str:
    """Decode an Atmoph UTF-8 payload."""
    return bytes(payload).decode("utf-8").strip("\x00\r\n ")


def encode_command(name: str) -> bytes:
    """Encode a named remote-control command."""
    try:
        return COMMANDS[name]
    except KeyError as err:
        raise ValueError(f"Unknown Atmoph command: {name}") from err


def encode_setting(name: str, value: bool | int | str) -> bytes:
    """Encode a single quick-menu setting exactly as the Android app does."""
    if name not in SETTING_KEYS:
        raise ValueError(f"Unknown Atmoph setting: {name}")
    return json.dumps({name: value}, separators=(",", ":")).encode()
