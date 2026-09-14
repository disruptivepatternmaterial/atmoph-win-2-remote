"""The dump as a data structure, what it means, and how it reads on the page.

Everything here works on dataclasses and plain dicts, so a window can be
described, judged, and rendered without a Bluetooth adapter anywhere near it.
That is where the boundary is: the transport in atmoph_diag.py produces these
objects and nothing here knows how it did, which is what lets the LED
reasoning and the report format be verified offline by tests/tools/test_diag.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from atmoph_catalog import (
    DECORATION_KEY,
    HEX_THRESHOLD,
    IDENTITY_CHARACTERISTICS,
    LED_KEY,
    LED_VERDICTS,
    MASK,
    MAX_VALUE_BYTES,
    REQUESTED_MTU,
    SCREEN_BRIGHTNESS_KEY,
    SECOND_SERVICE_UUID,
    SOUND_ONLY_KEY,
)
from atmoph_wire import QUICK_SETTINGS_UUID, SETTING_KEYS, Level, decode_text


@dataclass
class Target:
    """One window as the scanner currently sees it."""

    address: str
    name: str | None = None
    rssi: int | None = None
    service_uuids: list[str] = field(default_factory=list)
    matched_service: bool = False
    device: Any = None


@dataclass
class ValueDump:
    """A characteristic or descriptor value rendered both ways."""

    length: int | None = None
    text: str | None = None
    hex: str | None = None
    truncated: bool = False
    error: str | None = None


@dataclass
class DescriptorDump:
    """One descriptor of one characteristic."""

    uuid: str
    handle: int
    description: str | None = None
    value: ValueDump | None = None


@dataclass
class CharacteristicDump:
    """One characteristic, its declared properties, and its value."""

    uuid: str
    handle: int
    properties: list[str]
    label: str | None = None
    description: str | None = None
    value: ValueDump | None = None
    descriptors: list[DescriptorDump] = field(default_factory=list)


@dataclass
class ServiceDump:
    """One primary service and everything beneath it."""

    uuid: str
    handle: int
    label: str | None = None
    characteristics: list[CharacteristicDump] = field(default_factory=list)


@dataclass
class SettingDump:
    """One quick-settings key as the window reports it."""

    key: str
    kind: str
    value: object | None = None
    minimum: int | None = None
    maximum: int | None = None


@dataclass
class LedFinding:
    """The conclusion the quick-settings document supports about the LEDs."""

    verdict: str
    detail: str
    gates: list[str] = field(default_factory=list)


@dataclass
class WriteTest:
    """Result of writing one setting and watching for the echo."""

    key: str
    requested: object
    written: bool = False
    error: str | None = None
    echoed: bool = False
    echoed_value: object | None = None
    elapsed: float | None = None
    verdict: str = "not-attempted"
    previous: object | None = None
    restored: bool = False


@dataclass
class WindowDump:
    """Everything one run learned about one window."""

    address: str
    generated: str
    advertised_name: str | None = None
    device_name: str | None = None
    device_uuid: str | None = None
    rssi: int | None = None
    advertised_service_uuids: list[str] = field(default_factory=list)
    mtu_requested: int = REQUESTED_MTU
    mtu_negotiated: int | None = None
    connected: bool = False
    services: list[ServiceDump] = field(default_factory=list)
    quick_settings: dict[str, object] = field(default_factory=dict)
    quick_settings_source: list[str] = field(default_factory=list)
    settings: list[SettingDump] = field(default_factory=list)
    led: LedFinding | None = None
    writes: list[WriteTest] = field(default_factory=list)
    notifications: int = 0
    errors: list[str] = field(default_factory=list)


def render_value(raw: bytes) -> ValueDump:
    """Render a value as UTF-8 when it decodes cleanly, hex otherwise.

    The whole value is decoded and the resulting text shortened afterwards,
    rather than the bytes being cut first. Cutting first can land mid
    codepoint, which makes valid UTF-8 raise and be reported as undecodable
    binary - a statement about this tool's display limit dressed up as a
    finding about the window.
    """
    body = bytes(raw[:MAX_VALUE_BYTES])
    try:
        text: str | None = decode_text(bytes(raw))
    except UnicodeDecodeError:
        text = None
    if text and not all(character.isprintable() for character in text):
        text = None
    if text is not None:
        text = text[:MAX_VALUE_BYTES]
    return ValueDump(
        length=len(raw),
        text=text,
        hex=body.hex(" "),
        truncated=len(raw) > len(body),
    )


def describe_settings(document: dict[str, object]) -> list[SettingDump]:
    """Describe every key the app knows plus anything unexpected the window sent."""
    dumps: list[SettingDump] = []
    for key in sorted(SETTING_KEYS | set(document)):
        if key not in document:
            dumps.append(SettingDump(key=key, kind="absent"))
            continue
        raw = document[key]
        level = Level.from_wire(raw)
        if level is not None:
            dumps.append(
                SettingDump(
                    key=key,
                    kind="level",
                    value=level.value,
                    minimum=level.minimum,
                    maximum=level.maximum,
                )
            )
        elif isinstance(raw, bool):
            dumps.append(SettingDump(key=key, kind="bool", value=raw))
        elif isinstance(raw, dict):
            dumps.append(SettingDump(key=key, kind="malformed", value=raw))
        else:
            dumps.append(SettingDump(key=key, kind="other", value=raw))
    return dumps


def _led_gates(document: dict[str, object]) -> list[str]:
    """Report the settings that could plausibly hold the LEDs off."""
    gates: list[str] = []
    sound_only = document.get(SOUND_ONLY_KEY)
    if sound_only is True:
        gates.append(
            f"{SOUND_ONLY_KEY} is true: the unit is in audio-only mode, which "
            "may darken the panel and the LEDs by design. Turn it off and "
            "re-read before concluding anything."
        )
    elif sound_only is False:
        gates.append(f"{SOUND_ONLY_KEY} is false, so audio-only mode is not a gate")
    else:
        gates.append(f"{SOUND_ONLY_KEY} was not reported")

    decoration = Level.from_wire(document.get(DECORATION_KEY))
    if decoration is not None:
        gates.append(
            f"{DECORATION_KEY} is {decoration.value} of "
            f"{decoration.minimum}-{decoration.maximum}; whether a decoration "
            "can gate the LEDs is unverified, so step it if the LED range "
            "looks normal and the LEDs stay dark"
        )
    elif DECORATION_KEY in document:
        gates.append(f"{DECORATION_KEY} is {document[DECORATION_KEY]!r}, not a level")
    else:
        gates.append(f"{DECORATION_KEY} was not reported")

    present = sorted(SETTING_KEYS & set(document))
    gates.append(
        f"{len(present)} of {len(SETTING_KEYS)} known keys present, so the "
        "document is "
        + ("populated" if SCREEN_BRIGHTNESS_KEY in document else "suspect")
    )
    return gates


def analyse_leds(document: dict[str, object]) -> LedFinding:
    """Decide what the quick-settings document says about the LEDs."""
    gates = _led_gates(document) if document else []
    if not document:
        return LedFinding("no-document", LED_VERDICTS["no-document"], gates)
    if LED_KEY not in document:
        return LedFinding("key-absent", LED_VERDICTS["key-absent"], gates)
    level = Level.from_wire(document[LED_KEY])
    if level is None:
        return LedFinding("malformed", LED_VERDICTS["malformed"], gates)
    if level.maximum <= 0:
        return LedFinding("zero-max", LED_VERDICTS["zero-max"], gates)
    if level.value <= 0:
        return LedFinding("range-off", LED_VERDICTS["range-off"], gates)
    return LedFinding("range-on", LED_VERDICTS["range-on"], gates)


def normalize(dump: WindowDump) -> WindowDump:
    """Strip everything that differs between two runs or two units.

    The BLE address is a rotating resolvable private address, the signal
    strength and timings are noise, and the identity values are per-unit. What
    is left is the structure and the settings, which is what a diff is for.
    """
    services = [
        replace(
            service,
            characteristics=[
                replace(
                    characteristic,
                    value=(
                        ValueDump(text=MASK)
                        if characteristic.uuid in IDENTITY_CHARACTERISTICS
                        and characteristic.value is not None
                        else characteristic.value
                    ),
                )
                for characteristic in service.characteristics
            ],
        )
        for service in dump.services
    ]
    return replace(
        dump,
        address=MASK,
        generated=MASK,
        advertised_name=MASK if dump.advertised_name else None,
        device_name=MASK if dump.device_name else None,
        device_uuid=MASK if dump.device_uuid else None,
        rssi=None,
        mtu_negotiated=None,
        services=services,
        writes=[replace(write, elapsed=None) for write in dump.writes],
        notifications=0,
    )


def _format_value(value: ValueDump | None, indent: str, full_hex: bool) -> list[str]:
    if value is None:
        return []
    if value.error is not None:
        return [f"{indent}read failed: {value.error}"]
    lines = []
    if value.text is not None:
        lines.append(f"{indent}text {value.text!r}")
    # A long value that decoded cleanly is already readable, and its hex
    # doubles the size of the report for nothing.
    verbose = full_hex or value.text is None or (value.length or 0) <= HEX_THRESHOLD
    if value.hex is not None and verbose:
        suffix = " (truncated)" if value.truncated else ""
        length = "" if value.length is None else f"{value.length} bytes: "
        lines.append(f"{indent}raw  {length}{value.hex}{suffix}")
    elif value.hex is not None:
        lines.append(f"{indent}raw  {value.length} bytes, hex omitted (--hex shows it)")
    return lines


def _format_settings(dump: WindowDump) -> list[str]:
    lines = [f"-- quick settings ({QUICK_SETTINGS_UUID}) --"]
    source = ", ".join(dump.quick_settings_source) or "nothing"
    lines.append(f"obtained from {source}; {dump.notifications} notification(s)")
    if not dump.settings:
        lines.append("  no settings reported")
        return lines
    for setting in dump.settings:
        if setting.kind == "absent":
            lines.append(f"  {setting.key:<24} ABSENT")
        elif setting.kind == "level":
            lines.append(
                f"  {setting.key:<24} level   min {setting.minimum:<5}"
                f"max {setting.maximum:<5}value {setting.value}"
            )
        else:
            lines.append(
                f"  {setting.key:<24} {setting.kind:<8}{json.dumps(setting.value)}"
            )
    return lines


def _format_led(dump: WindowDump) -> list[str]:
    if dump.led is None:
        return []
    lines = ["-- LED finding --", f"verdict {dump.led.verdict}", dump.led.detail]
    lines += [f"  gate: {gate}" for gate in dump.led.gates]
    return lines


def _format_writes(dump: WindowDump) -> list[str]:
    if not dump.writes:
        return []
    lines = ["-- write tests --"]
    for write in dump.writes:
        elapsed = "-" if write.elapsed is None else f"{write.elapsed}s"
        lines.append(
            f"  {write.key} = {json.dumps(write.requested)} -> {write.verdict} "
            f"(echo after {elapsed})"
        )
        if write.error:
            lines.append(f"      error {write.error}")
        if write.echoed:
            lines.append(f"      echoed value {json.dumps(write.echoed_value)}")
        if write.previous is not None:
            restored = "restore confirmed" if write.restored else "not restored"
            lines.append(
                f"      previous value {json.dumps(write.previous)} ({restored})"
            )
    return lines


def _format_gatt(dump: WindowDump, full_hex: bool) -> list[str]:
    lines = ["-- GATT table --"]
    for service in dump.services:
        label = f"  [{service.label}]" if service.label else ""
        lines.append(f"service {service.uuid} handle {service.handle}{label}")
        for characteristic in service.characteristics:
            properties = ",".join(characteristic.properties) or "none"
            label = f"  {characteristic.label}" if characteristic.label else ""
            lines.append(
                f"  char {characteristic.uuid} handle "
                f"{characteristic.handle:<5}{properties}{label}"
            )
            lines += _format_value(characteristic.value, "      ", full_hex)
            for descriptor in characteristic.descriptors:
                description = (
                    f"  {descriptor.description}" if descriptor.description else ""
                )
                lines.append(
                    f"      descriptor {descriptor.uuid} handle "
                    f"{descriptor.handle}{description}"
                )
                lines += _format_value(descriptor.value, "          ", full_hex)
    return lines


def format_dump(dump: WindowDump, full_hex: bool = False) -> str:
    """Render one window as sorted, diff-friendly text."""
    readable = sum(
        1
        for service in dump.services
        for characteristic in service.characteristics
        if "read" in characteristic.properties
    )
    failures = sum(
        1
        for service in dump.services
        for characteristic in service.characteristics
        if characteristic.value is not None and characteristic.value.error is not None
    )
    characteristics = sum(len(service.characteristics) for service in dump.services)
    has_second = any(s.uuid == SECOND_SERVICE_UUID for s in dump.services)
    mtu = dump.mtu_negotiated if dump.mtu_negotiated is not None else "unknown"

    lines = [
        f"== window {dump.advertised_name or dump.device_name or dump.address} ==",
        f"address           {dump.address}  (resolvable private, rotates)",
        f"advertised name   {dump.advertised_name or '-'}",
        f"device name       {dump.device_name or '-'}",
        f"device uuid       {dump.device_uuid or '-'}",
        f"rssi              {'-' if dump.rssi is None else f'{dump.rssi} dBm'}",
        f"advertised uuids  {', '.join(dump.advertised_service_uuids) or '-'}",
        f"mtu               requested {dump.mtu_requested}, negotiated {mtu}",
        f"connected         {dump.connected}",
        f"generated         {dump.generated}",
        f"second service    {'present' if has_second else 'absent'}",
        f"summary           {len(dump.services)} service(s), {characteristics} "
        f"characteristic(s), {readable} readable, {failures} read failure(s)",
    ]
    for error in dump.errors:
        lines.append(f"error             {error}")
    lines.append("")
    lines += _format_settings(dump)
    lines.append("")
    lines += _format_led(dump)
    writes = _format_writes(dump)
    if writes:
        lines.append("")
        lines += writes
    lines.append("")
    lines += _format_gatt(dump, full_hex)
    return "\n".join(lines)


def report_header() -> str:
    """Warn about what the dump contains before anyone shares it."""
    return (
        "# Atmoph Window BLE diagnostic dump\n"
        "# WARNING: this dump contains BLE addresses, device UUIDs, and device\n"
        "# names. This repository and its issues are public. Review the file\n"
        "# before sharing it, or re-run with --normalize, which masks identity\n"
        "# and volatile fields and leaves the structure a diff needs."
    )


def slug(dump: WindowDump) -> str:
    """Build a stable per-window filename stem."""
    for candidate in (
        dump.device_uuid,
        dump.advertised_name,
        dump.device_name,
        dump.address,
    ):
        if candidate and candidate != MASK:
            cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("-").lower()
            if cleaned:
                return cleaned
    return "window"
