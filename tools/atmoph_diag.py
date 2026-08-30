#!/usr/bin/env python3
"""BLE diagnostic dump for Atmoph Window devices.

Produces the evidence needed to explain a misbehaving window from outside its
firmware: the complete GATT table, every readable value, and the quick-settings
document that reveals whether the firmware believes the unit has LEDs at all.

Several units can be dumped in one run and their reports diffed, which is the
fastest way to separate a broken window from a working one.

Subcommands:
    scan                 active-scan for windows advertising the vendor service
    dump [ADDRESS ...]   connect and dump; scans first when given no address

Reads are unconditional. No application payload is written unless --provoke,
--led-write, or --write is given. Subscribing to notifications does write a
client characteristic configuration descriptor, but that is transport state and
changes nothing on the device.

Parsing and report formatting are verified offline against a fake peripheral by
tools/selftest.py.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.machinery
import importlib.util
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _register_namespace(name: str, path: Path) -> None:
    """Put a package in sys.modules without executing its __init__.py.

    `custom_components/atmoph_window/__init__.py` is the Home Assistant entry
    point, so importing the package normally pulls in Home Assistant. The wire
    protocol imports none of it, and this tool has to run on a laptop with
    nothing but bleak installed, so the package objects are registered directly
    and the submodule loads against them. `tests/conftest.py` does the same
    thing for the same reason.
    """
    if name in sys.modules:
        return
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    sys.modules[name] = importlib.util.module_from_spec(spec)


if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_register_namespace("custom_components", _REPO_ROOT / "custom_components")
_register_namespace(
    "custom_components.atmoph_window",
    _REPO_ROOT / "custom_components" / "atmoph_window",
)

from custom_components.atmoph_window.protocol import (  # noqa: E402
    COMMAND_UUID,
    IDENTITY_UUID,
    QUICK_SETTINGS_UUID,
    SERVICE_UUID,
    SETTING_KEYS,
    JsonObjectStream,
    Level,
    decode_text,
    encode_command,
    encode_setting,
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
KNOWN_CHARACTERISTICS: dict[str, str] = {
    IDENTITY_UUID: "app: device UUID and name",
    "ec812b51-ae67-4cf3-8272-3967b3fc22a0": "app: panorama role",
    "1d862803-b301-4548-bece-1f1ab61881b8": "app: current view title",
    "99cd2547-0640-485c-9996-e0a2b384a6f2": "app: current view image URL",
    "275ddae2-4c69-4638-97d4-d5ba8e9e05d1": "app: current view location",
    "3afad096-8cb5-4cb7-a8b4-c7dca3e41b94": "app: focused UI element JSON",
    COMMAND_UUID: "app: remote control commands",
    "73de7799-b573-46f3-99fd-c6a7a8fc2fde": "app: text input JSON",
    QUICK_SETTINGS_UUID: "app: quick settings JSON",
    "7607f5a4-22bc-4730-9019-c78dc8b50341": "app: display power",
    "03cffbfe-b23a-4c8f-bf57-9591b4d59119": "declared: view id and revision",
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


class ScanUnavailable(RuntimeError):
    """The host refused to scan, which is not the same as finding nothing."""


def _import_bleak() -> tuple[Any, Any]:
    """Import bleak lazily so --help works on a host without it installed."""
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as err:  # pragma: no cover - environment dependent
        raise SystemExit(
            "bleak is required for scan and dump: pip install bleak"
        ) from err
    return BleakClient, BleakScanner


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
    """Render a value as UTF-8 when it decodes cleanly, hex otherwise."""
    body = bytes(raw[:MAX_VALUE_BYTES])
    try:
        text: str | None = decode_text(body)
    except UnicodeDecodeError:
        text = None
    if text and not all(character.isprintable() for character in text):
        text = None
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


class SettingsWatcher:
    """Reassemble quick-settings documents arriving on the notify channel."""

    def __init__(self) -> None:
        self._stream = JsonObjectStream()
        self.merged: dict[str, object] = {}
        self.count = 0
        self._queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def feed(self, payload: bytes) -> None:
        """Accept one notification, ignoring a payload that never parses."""
        try:
            documents = self._stream.feed(payload)
        except ValueError:
            return
        for document in documents:
            self.count += 1
            self.merged.update(document)
            self._queue.put_nowait(document)

    async def wait_for_key(
        self, key: str, timeout: float
    ) -> tuple[object | None, float]:
        """Wait for the next document mentioning a key, returning its value."""
        started = time.monotonic()
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return None, time.monotonic() - started
            try:
                document = await asyncio.wait_for(self._queue.get(), remaining)
            except TimeoutError:
                return None, time.monotonic() - started
            if key in document:
                return document[key], time.monotonic() - started


async def discover(
    timeout: float, adapter: str | None, unfiltered: bool
) -> list[Target]:
    """Actively scan for windows, merging the scan response into each result.

    The advertised name rides in the scan response rather than the
    advertisement, so the same window is seen named and nameless seconds apart.
    Detections are merged per address so a name observed once is kept.
    """
    _, scanner_class = _import_bleak()
    seen: dict[str, Target] = {}

    def detected(device: Any, advertisement: Any) -> None:
        uuids = sorted({str(u).lower() for u in (advertisement.service_uuids or [])})
        matched = SERVICE_UUID in uuids
        name = device.name or advertisement.local_name or None
        target = seen.get(device.address)
        if target is None:
            if not (matched or unfiltered):
                return
            seen[device.address] = Target(
                address=device.address,
                name=name,
                rssi=advertisement.rssi,
                service_uuids=uuids,
                matched_service=matched,
                device=device,
            )
            return
        target.device = device
        target.rssi = advertisement.rssi
        target.matched_service = target.matched_service or matched
        target.service_uuids = sorted(set(target.service_uuids) | set(uuids))
        if name:
            target.name = name

    kwargs: dict[str, Any] = {"scanning_mode": "active"}
    if not unfiltered:
        kwargs["service_uuids"] = [SERVICE_UUID]
    if adapter:
        kwargs["adapter"] = adapter

    scanner = scanner_class(detection_callback=detected, **kwargs)
    try:
        await scanner.start()
    except Exception as exc:
        raise ScanUnavailable(
            f"this host will not scan ({type(exc).__name__}: {exc}). That is not "
            "the same as finding no window: macOS refuses Bluetooth to a "
            "sandboxed process, so run this from a normal terminal that has been "
            "granted the Bluetooth permission, and check the adapter is on."
        ) from exc
    try:
        await asyncio.sleep(timeout)
    finally:
        with contextlib.suppress(Exception):
            await scanner.stop()
    return sorted(seen.values(), key=lambda t: (t.name or "\uffff", t.address))


async def _read_characteristic(client: Any, characteristic: Any) -> ValueDump:
    try:
        raw = await client.read_gatt_char(characteristic)
    except Exception as exc:
        return ValueDump(error=f"{type(exc).__name__}: {exc}")
    return render_value(bytes(raw))


async def collect_gatt(client: Any, read_descriptors: bool) -> list[ServiceDump]:
    """Enumerate every service, characteristic, descriptor, and readable value."""
    services: list[ServiceDump] = []
    for service in client.services:
        characteristics: list[CharacteristicDump] = []
        for characteristic in service.characteristics:
            properties = sorted(characteristic.properties)
            value = None
            if "read" in properties:
                value = await _read_characteristic(client, characteristic)
            descriptors: list[DescriptorDump] = []
            for descriptor in characteristic.descriptors:
                dumped = DescriptorDump(
                    uuid=str(descriptor.uuid).lower(),
                    handle=descriptor.handle,
                    description=getattr(descriptor, "description", None) or None,
                )
                if read_descriptors:
                    try:
                        raw = await client.read_gatt_descriptor(descriptor.handle)
                    except Exception as exc:
                        dumped.value = ValueDump(error=f"{type(exc).__name__}: {exc}")
                    else:
                        dumped.value = render_value(bytes(raw))
                descriptors.append(dumped)
            uuid = str(characteristic.uuid).lower()
            characteristics.append(
                CharacteristicDump(
                    uuid=uuid,
                    handle=characteristic.handle,
                    properties=properties,
                    label=KNOWN_CHARACTERISTICS.get(uuid),
                    description=getattr(characteristic, "description", None) or None,
                    value=value,
                    descriptors=sorted(descriptors, key=lambda d: (d.uuid, d.handle)),
                )
            )
        uuid = str(service.uuid).lower()
        services.append(
            ServiceDump(
                uuid=uuid,
                handle=service.handle,
                label=SERVICE_LABELS.get(uuid),
                characteristics=sorted(
                    characteristics, key=lambda c: (c.uuid, c.handle)
                ),
            )
        )
    return sorted(services, key=_service_order)


def _service_order(service: ServiceDump) -> tuple[int, str, int]:
    """Order the two vendor services first, then sort the rest by UUID.

    Handles are not stable across firmware revisions, so a diff needs a sort
    that does not depend on them.
    """
    rank = {SERVICE_UUID: 0, SECOND_SERVICE_UUID: 1}.get(service.uuid, 2)
    return rank, service.uuid, service.handle


def find_value(services: list[ServiceDump], uuid: str) -> ValueDump | None:
    """Return the value already read for a characteristic, if it was readable."""
    for service in services:
        for characteristic in service.characteristics:
            if characteristic.uuid == uuid and characteristic.value is not None:
                return characteristic.value
    return None


def _parse_document(value: ValueDump | None) -> dict[str, object] | None:
    if value is None or not value.text:
        return None
    try:
        parsed = json.loads(value.text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _run_write_test(
    client: Any,
    watcher: SettingsWatcher,
    known: dict[str, object],
    key: str,
    value: bool | int | str,
    echo_timeout: float,
    restore: bool,
) -> WriteTest:
    """Write one setting, watch for the echo, and put the old value back.

    A setting write is echoed on the notify channel, which makes this the only
    reliable way to prove the write path works: an observed view change does
    not, because DailyRoutineEnable rotates views on its own.
    """
    previous = known.get(key)
    level = Level.from_wire(previous)
    test = WriteTest(
        key=key,
        requested=value,
        previous=level.value if level is not None else previous,
    )
    try:
        await client.write_gatt_char(
            QUICK_SETTINGS_UUID, encode_setting(key, value), response=True
        )
    except Exception as exc:
        test.error = f"{type(exc).__name__}: {exc}"
        test.verdict = "write-rejected"
        return test
    test.written = True

    echoed, elapsed = await watcher.wait_for_key(key, echo_timeout)
    test.elapsed = round(elapsed, 3)
    if echoed is None:
        test.verdict = "no-echo"
    else:
        test.echoed = True
        echoed_level = Level.from_wire(echoed)
        test.echoed_value = echoed_level.value if echoed_level else echoed
        test.verdict = "echo-matched" if test.echoed_value == value else "echo-clamped"

    if (
        restore
        and test.written
        and isinstance(test.previous, bool | int | str)
        and test.previous != value
    ):
        with contextlib.suppress(Exception):
            await client.write_gatt_char(
                QUICK_SETTINGS_UUID,
                encode_setting(key, test.previous),
                response=True,
            )
            confirmed, _ = await watcher.wait_for_key(key, echo_timeout)
            test.restored = confirmed is not None
    return test


async def collect_dump(
    client: Any,
    target: Target,
    *,
    provoke: bool = False,
    writes: list[tuple[str, bool | int | str]] | None = None,
    echo_timeout: float = 6.0,
    read_descriptors: bool = True,
    restore: bool = True,
) -> WindowDump:
    """Dump one already-connected window."""
    dump = WindowDump(
        address=target.address,
        generated=datetime.now(UTC).isoformat(timespec="seconds"),
        advertised_name=target.name,
        rssi=target.rssi,
        advertised_service_uuids=list(target.service_uuids),
        connected=bool(getattr(client, "is_connected", True)),
        mtu_negotiated=getattr(client, "mtu_size", None),
    )

    watcher = SettingsWatcher()
    subscribed = False
    try:
        await client.start_notify(
            QUICK_SETTINGS_UUID, lambda _sender, data: watcher.feed(bytes(data))
        )
        subscribed = True
    except Exception as exc:
        dump.errors.append(f"quick-settings notify unavailable: {exc}")

    try:
        dump.services = await collect_gatt(client, read_descriptors)

        identity = find_value(dump.services, IDENTITY_UUID)
        if identity is not None and identity.text:
            parts = identity.text.split(",", 1)
            dump.device_uuid = parts[0] or None
            dump.device_name = parts[1] if len(parts) > 1 and parts[1] else None

        document = _parse_document(find_value(dump.services, QUICK_SETTINGS_UUID))
        if document is not None:
            dump.quick_settings.update(document)
            dump.quick_settings_source.append("read")

        if provoke:
            try:
                await client.write_gatt_char(
                    COMMAND_UUID, encode_command("connect_notify"), response=True
                )
            except Exception as exc:
                dump.errors.append(f"state request write failed: {exc}")
            else:
                await watcher.wait_for_key(SCREEN_BRIGHTNESS_KEY, echo_timeout)

        if watcher.merged:
            dump.quick_settings.update(watcher.merged)
            dump.quick_settings_source.append("notify")

        # The findings describe the window as it was found. A write test would
        # otherwise overwrite the evidence it exists to explain, so it reports
        # its own before and after values instead.
        found = dict(dump.quick_settings)
        for key, value in writes or []:
            found.update(watcher.merged)
            dump.writes.append(
                await _run_write_test(
                    client, watcher, found, key, value, echo_timeout, restore
                )
            )
    finally:
        if subscribed:
            with contextlib.suppress(Exception):
                await client.stop_notify(QUICK_SETTINGS_UUID)

    dump.notifications = watcher.count
    dump.settings = describe_settings(dump.quick_settings)
    dump.led = analyse_leds(dump.quick_settings)
    return dump


async def dump_window(
    target: Target,
    *,
    timeout: float,
    provoke: bool,
    writes: list[tuple[str, bool | int | str]],
    echo_timeout: float,
    read_descriptors: bool,
    restore: bool,
) -> WindowDump:
    """Connect to one window and dump it, reporting a failure as a dump."""
    client_class, _ = _import_bleak()
    client = client_class(target.device or target.address, timeout=timeout)
    try:
        await client.connect()
    except Exception as exc:
        return WindowDump(
            address=target.address,
            generated=datetime.now(UTC).isoformat(timespec="seconds"),
            advertised_name=target.name,
            rssi=target.rssi,
            advertised_service_uuids=list(target.service_uuids),
            errors=[f"connect failed: {type(exc).__name__}: {exc}"],
        )
    try:
        return await collect_dump(
            client,
            target,
            provoke=provoke,
            writes=writes,
            echo_timeout=echo_timeout,
            read_descriptors=read_descriptors,
            restore=restore,
        )
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


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


def parse_write_argument(text: str) -> tuple[str, bool | int | str]:
    """Parse a KEY=VALUE write request, typing the value as the app does."""
    key, separator, raw = text.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    key = key.strip()
    if key not in SETTING_KEYS:
        known = ", ".join(sorted(SETTING_KEYS))
        raise argparse.ArgumentTypeError(f"unknown setting {key!r}; known: {known}")
    raw = raw.strip()
    if raw.lower() in {"true", "false"}:
        return key, raw.lower() == "true"
    try:
        return key, int(raw)
    except ValueError:
        return key, raw


def build_parser() -> argparse.ArgumentParser:
    """Build the command line, kept close to tools/atmoph_netscan.py."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable output"
    )
    parser.add_argument(
        "--normalize",
        action="store_true",
        help="mask volatile and identity fields so dumps diff cleanly",
    )
    parser.add_argument("--adapter", help="Bluetooth adapter, BlueZ hosts only")
    parser.add_argument(
        "--scan-timeout", type=float, default=12.0, help="seconds to scan"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="active-scan for windows")
    p_scan.add_argument(
        "--unfiltered",
        action="store_true",
        help="report every advertiser, not only windows",
    )

    p_dump = sub.add_parser("dump", help="connect and dump the full GATT table")
    p_dump.add_argument("addresses", nargs="*", help="skip the scan and use these")
    p_dump.add_argument(
        "--name",
        action="append",
        default=[],
        help="only dump discovered windows with this advertised name",
    )
    p_dump.add_argument(
        "--max", type=int, default=0, help="stop after this many windows"
    )
    p_dump.add_argument(
        "--timeout", type=float, default=30.0, help="per-connect timeout"
    )
    p_dump.add_argument(
        "--echo-timeout",
        type=float,
        default=6.0,
        help="seconds to wait for a notification echo",
    )
    p_dump.add_argument(
        "--no-descriptors",
        action="store_true",
        help="enumerate descriptors without reading them",
    )
    p_dump.add_argument(
        "--hex",
        action="store_true",
        help="print the hex of every value, not only the undecodable ones",
    )
    p_dump.add_argument(
        "--provoke",
        action="store_true",
        help="write the app's C state request so the window announces settings",
    )
    p_dump.add_argument(
        "--led-write",
        type=int,
        metavar="N",
        help=f'write {{"{LED_KEY}": N}} and report whether it is echoed',
    )
    p_dump.add_argument(
        "--write",
        action="append",
        default=[],
        type=parse_write_argument,
        metavar="KEY=VALUE",
        help="write any known setting and report the echo, repeatable",
    )
    p_dump.add_argument(
        "--no-restore",
        action="store_true",
        help="leave written settings at their new value",
    )
    p_dump.add_argument("--out", help="write one report file per window into DIR")
    return parser


def _emit(payload: object, as_json: bool, text: str) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else text)


async def _command_scan(args: argparse.Namespace) -> int:
    targets = await discover(args.scan_timeout, args.adapter, args.unfiltered)
    lines = [f"{len(targets)} device(s) in {args.scan_timeout:.0f}s of active scanning"]
    for target in targets:
        marker = "window" if target.matched_service else "other"
        rssi = "-" if target.rssi is None else f"{target.rssi} dBm"
        lines.append(
            f"  {target.address:<40} {target.name or '(nameless)':<24} "
            f"{rssi:<9} {marker}"
        )
    if not targets:
        lines.append(
            "  nothing found. The name rides in the scan response, so a window "
            "can appear nameless; scan longer, close the phone app, and check "
            "that this host can actually scan before concluding it is absent."
        )
    _emit([asdict(t) | {"device": None} for t in targets], args.json, "\n".join(lines))
    return 0


async def _resolve_targets(args: argparse.Namespace) -> list[Target]:
    if args.addresses:
        return [Target(address=address) for address in args.addresses]
    targets = [
        target
        for target in await discover(args.scan_timeout, args.adapter, False)
        if target.matched_service
    ]
    if args.name:
        wanted = {name.lower() for name in args.name}
        targets = [t for t in targets if (t.name or "").lower() in wanted]
    if args.max > 0:
        targets = targets[: args.max]
    return targets


async def _command_dump(args: argparse.Namespace) -> int:
    writes: list[tuple[str, bool | int | str]] = list(args.write)
    if args.led_write is not None:
        writes.insert(0, (LED_KEY, args.led_write))

    targets = await _resolve_targets(args)
    if not targets:
        print(
            "no window found. Scan first with the scan subcommand, or pass an "
            "address explicitly. Addresses rotate, so a stale one will fail.",
            file=sys.stderr,
        )
        return 1

    # The filename has to come from the real identity even when the contents
    # are masked, or every normalized dump would land on the same path.
    dumps: list[tuple[WindowDump, WindowDump]] = []
    for target in targets:
        dump = await dump_window(
            target,
            timeout=args.timeout,
            provoke=args.provoke,
            writes=writes,
            echo_timeout=args.echo_timeout,
            read_descriptors=not args.no_descriptors,
            restore=not args.no_restore,
        )
        dumps.append((dump, normalize(dump) if args.normalize else dump))

    if args.out:
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        suffix = "json" if args.json else "txt"
        for identity, dump in dumps:
            body = (
                json.dumps(asdict(dump), indent=2, sort_keys=True)
                if args.json
                else f"{report_header()}\n\n{format_dump(dump, args.hex)}"
            )
            path = directory / f"{slug(identity)}.{suffix}"
            path.write_text(f"{body}\n", encoding="utf-8")
            print(f"wrote {path}", file=sys.stderr)

    presented = [dump for _, dump in dumps]
    text = "\n\n".join(
        [report_header()] + [format_dump(dump, args.hex) for dump in presented]
    )
    _emit([asdict(dump) for dump in presented], args.json, text)
    return 0 if any(dump.connected for dump in presented) else 1


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            return await _command_scan(args)
        if args.command == "dump":
            return await _command_dump(args)
    except ScanUnavailable as err:
        print(err, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
