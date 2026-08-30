#!/usr/bin/env python3
"""Offline verification for the diagnostics in tools/.

Neither tool can reach real hardware from a sandbox: Bluetooth is refused to a
sandboxed process and LAN TCP connects are dropped. So the parts that can be
wrong on their own — value rendering, LED reasoning, write-echo handling, report
normalization, and the ADB handshake — are checked here against a fake window
and a fake adbd rather than against a device.

Run it directly; it prints every failed expectation and exits non-zero:

    python3 tools/selftest.py
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import atmoph_diag as diag  # noqa: E402
import atmoph_netscan as netscan  # noqa: E402

WORKING_SETTINGS: dict[str, object] = {
    "WidgetsVisible": True,
    "DailyRoutineEnable": False,
    "LandscapeVolumeLevel": {"min": 0, "max": 24, "value": 12},
    "SoundscapeLayer": {"min": 0, "max": 5, "value": 1},
    "SoundscapeVolumeLevel": {"min": 0, "max": 20, "value": 4},
    "ScreenBrightness": {"min": 1, "max": 25, "value": 18},
    "CurrentDecoration": {"min": 0, "max": 19, "value": 3},
    "SoundOnly": False,
    "LedBrightness": {"min": 0, "max": 20, "value": 7},
}

# The main service as docs/PROTOCOL.md maps it, trimmed to what the report
# needs to exercise: an identity value, a JSON setting, and a write-only entry.
MAIN_CHARACTERISTICS = (
    (diag.IDENTITY_UUID, ["read"], b"0f8c1d3a-2b4c-4e6f-9a1b-2c3d4e5f6071,Studio"),
    ("ec812b51-ae67-4cf3-8272-3967b3fc22a0", ["read", "notify"], b"None"),
    ("1d862803-b301-4548-bece-1f1ab61881b8", ["read", "notify"], b"Kamikochi"),
    ("7607f5a4-22bc-4730-9019-c78dc8b50341", ["read", "notify"], b"true"),
    (diag.COMMAND_UUID, ["write"], None),
    ("750b35af-a702-4407-95a9-5af779a61785", ["read", "write"], b"\x00\xff\x10\x80"),
)

SECOND_CHARACTERISTICS = (
    ("596f4372-1456-4038-8bca-19ef89e6fe3e", ["read", "write"], b'{"IsLocked":false}'),
    ("2330f10b-d28c-4b0e-89c7-8dbd05dfa491", ["write"], None),
)


@dataclass
class FakeDescriptor:
    """Stands in for a bleak descriptor."""

    uuid: str
    handle: int
    description: str | None = None
    value: bytes | None = None


@dataclass
class FakeCharacteristic:
    """Stands in for a bleak characteristic."""

    uuid: str
    handle: int
    properties: list[str]
    value: bytes | None = None
    description: str | None = None
    descriptors: list[FakeDescriptor] = field(default_factory=list)


@dataclass
class FakeService:
    """Stands in for a bleak service."""

    uuid: str
    handle: int
    characteristics: list[FakeCharacteristic] = field(default_factory=list)


class FakeWindow:
    """A window that answers reads, echoes setting writes, and can misbehave."""

    def __init__(
        self,
        settings: dict[str, object] | None = None,
        *,
        readable_settings: bool = True,
        echo: str = "match",
        clamp_to: object = 0,
        second_service: bool = True,
    ) -> None:
        self.settings = json.loads(json.dumps(settings or WORKING_SETTINGS))
        self.readable_settings = readable_settings
        self.echo = echo
        self.clamp_to = clamp_to
        self.is_connected = True
        self.mtu_size = 128
        self.writes: list[tuple[str, bytes]] = []
        self._callbacks: dict[str, Any] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self.services = self._build_services(second_service)

    def _build_services(self, second_service: bool) -> list[FakeService]:
        handle = 1
        services: list[FakeService] = []
        main = FakeService(uuid=diag.SERVICE_UUID, handle=handle)
        for uuid, properties, value in MAIN_CHARACTERISTICS:
            handle += 1
            main.characteristics.append(
                FakeCharacteristic(
                    uuid=uuid, handle=handle, properties=list(properties), value=value
                )
            )
        handle += 1
        main.characteristics.append(
            FakeCharacteristic(
                uuid=diag.QUICK_SETTINGS_UUID,
                handle=handle,
                properties=["read", "write", "notify"],
                descriptors=[
                    FakeDescriptor(
                        uuid="00002902-0000-1000-8000-00805f9b34fb",
                        handle=handle + 1,
                        description="Client Characteristic Configuration",
                        value=b"\x01\x00",
                    )
                ],
            )
        )
        handle += 2
        services.append(main)
        if second_service:
            second = FakeService(uuid=diag.SECOND_SERVICE_UUID, handle=handle)
            for uuid, properties, value in SECOND_CHARACTERISTICS:
                handle += 1
                second.characteristics.append(
                    FakeCharacteristic(
                        uuid=uuid,
                        handle=handle,
                        properties=list(properties),
                        value=value,
                    )
                )
            services.append(second)
        return services

    async def read_gatt_char(self, characteristic: FakeCharacteristic) -> bytearray:
        if characteristic.uuid == diag.QUICK_SETTINGS_UUID:
            if not self.readable_settings:
                raise RuntimeError("ATT read not permitted")
            return bytearray(json.dumps(self.settings).encode())
        if characteristic.value is None:
            raise RuntimeError("ATT read not permitted")
        return bytearray(characteristic.value)

    async def read_gatt_descriptor(self, handle: int) -> bytearray:
        for service in self.services:
            for characteristic in service.characteristics:
                for descriptor in characteristic.descriptors:
                    if descriptor.handle == handle and descriptor.value is not None:
                        return bytearray(descriptor.value)
        raise RuntimeError("ATT read not permitted")

    async def start_notify(self, uuid: str, callback: Any) -> None:
        self._callbacks[uuid] = callback

    async def stop_notify(self, uuid: str) -> None:
        self._callbacks.pop(uuid, None)

    async def write_gatt_char(
        self, uuid: str, data: bytes, response: bool = True
    ) -> None:
        del response
        self.writes.append((uuid, bytes(data)))
        if uuid == diag.COMMAND_UUID:
            if bytes(data) == b"C":
                self._announce()
            return
        if uuid != diag.QUICK_SETTINGS_UUID:
            raise RuntimeError("ATT write not permitted")
        payload = json.loads(data.decode())
        for key, value in payload.items():
            if self.echo == "reject":
                raise RuntimeError("ATT write not permitted")
            applied = self.clamp_to if self.echo == "clamp" else value
            current = self.settings.get(key)
            if isinstance(current, dict):
                self.settings[key] = {**current, "value": applied}
            else:
                self.settings[key] = applied
        if self.echo != "none":
            self._announce()

    def _announce(self) -> None:
        """Echo the whole document, split so reassembly is exercised."""
        callback = self._callbacks.get(diag.QUICK_SETTINGS_UUID)
        if callback is None:
            return
        body = json.dumps(self.settings).encode()
        middle = len(body) // 2

        async def deliver() -> None:
            await asyncio.sleep(0)
            callback(None, bytearray(body[:middle]))
            callback(None, bytearray(body[middle:]))

        self._tasks.append(asyncio.ensure_future(deliver()))

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks)
            self._tasks.clear()


class Report:
    """Collects expectation results so every failure is reported at once."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def expect(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            return
        self.failures.append(f"{name}{f': {detail}' if detail else ''}")

    def equal(self, name: str, actual: object, expected: object) -> None:
        self.expect(name, actual == expected, f"got {actual!r}, want {expected!r}")


def check_value_rendering(report: Report) -> None:
    text = diag.render_value(b"Kamikochi")
    report.equal("utf-8 renders as text", text.text, "Kamikochi")
    report.equal("utf-8 keeps its length", text.length, 9)

    binary = diag.render_value(b"\x00\xff\x10\x80")
    report.expect("binary has no text", binary.text is None, f"got {binary.text!r}")
    report.equal("binary renders as hex", binary.hex, "00 ff 10 80")

    padded = diag.render_value(b"ok\x00\x00")
    report.equal("nul padding is stripped from text", padded.text, "ok")
    report.equal("nul padding stays visible in hex", padded.hex, "6f 6b 00 00")

    long_value = diag.render_value(b"a" * (diag.MAX_VALUE_BYTES + 10))
    report.expect("an oversized value is flagged", long_value.truncated)
    report.equal("an oversized value keeps its real length", long_value.length, 522)

    control = diag.render_value(b"a\x07b")
    report.expect(
        "a control character falls back to hex",
        control.text is None,
        f"got {control.text!r}",
    )


def check_settings_description(report: Report) -> None:
    dumps = diag.describe_settings(
        {
            "ScreenBrightness": {"min": 1, "max": 25, "value": 18},
            "SoundOnly": False,
            "CurrentDecoration": "unexpected",
            "UnknownFromFirmware": 3,
        }
    )
    by_key = {dump.key: dump for dump in dumps}
    report.equal("every known key is described", len(dumps), len(diag.SETTING_KEYS) + 1)
    report.equal("keys are sorted", [d.key for d in dumps], sorted(by_key))
    report.equal("a level reports its kind", by_key["ScreenBrightness"].kind, "level")
    report.equal("a level reports max", by_key["ScreenBrightness"].maximum, 25)
    report.equal("a boolean reports its kind", by_key["SoundOnly"].kind, "bool")
    report.equal("a missing key reads absent", by_key["LedBrightness"].kind, "absent")
    report.equal(
        "a non-level string is not a level", by_key["CurrentDecoration"].kind, "other"
    )
    report.equal("an unexpected key survives", by_key["UnknownFromFirmware"].value, 3)

    malformed = diag.describe_settings({"LedBrightness": {"value": 3}})
    report.equal(
        "a level missing its bounds is malformed",
        {d.key: d.kind for d in malformed}["LedBrightness"],
        "malformed",
    )


def check_led_reasoning(report: Report) -> None:
    cases = {
        "no-document": {},
        "key-absent": {
            k: v for k, v in WORKING_SETTINGS.items() if k != "LedBrightness"
        },
        "zero-max": {
            **WORKING_SETTINGS,
            "LedBrightness": {"min": 0, "max": 0, "value": 0},
        },
        "malformed": {**WORKING_SETTINGS, "LedBrightness": 4},
        "range-off": {
            **WORKING_SETTINGS,
            "LedBrightness": {"min": 0, "max": 20, "value": 0},
        },
        "range-on": WORKING_SETTINGS,
    }
    for expected, document in cases.items():
        finding = diag.analyse_leds(document)
        report.equal(f"led verdict {expected}", finding.verdict, expected)
        report.expect(f"led verdict {expected} explains itself", bool(finding.detail))

    gated = diag.analyse_leds({**WORKING_SETTINGS, "SoundOnly": True})
    report.expect(
        "sound-only is reported as a gate",
        any("audio-only" in gate for gate in gated.gates),
        f"gates {gated.gates}",
    )
    report.expect(
        "the decoration is reported as a gate",
        any(gate.startswith("CurrentDecoration is 3") for gate in gated.gates),
        f"gates {gated.gates}",
    )
    report.expect("an empty document has no gates", diag.analyse_leds({}).gates == [])


async def check_dump_of_working_window(report: Report) -> None:
    window = FakeWindow()
    dump = await diag.collect_dump(
        window, diag.Target(address="AA:BB:CC:DD:EE:FF", name="Studio")
    )
    await window.drain()

    report.equal("both services are enumerated", len(dump.services), 2)
    report.equal(
        "the second service is found",
        [s.uuid for s in dump.services].count(diag.SECOND_SERVICE_UUID),
        1,
    )
    report.equal("the device uuid is parsed", dump.device_uuid[:8], "0f8c1d3a")
    report.equal("the device name is parsed", dump.device_name, "Studio")
    report.equal("the negotiated mtu is recorded", dump.mtu_negotiated, 128)
    report.equal(
        "quick settings come from the read", dump.quick_settings_source, ["read"]
    )
    report.equal("the led verdict is normal", dump.led.verdict, "range-on")
    report.equal("nothing was written", window.writes, [])

    labelled = [
        characteristic
        for service in dump.services
        for characteristic in service.characteristics
        if characteristic.label
    ]
    report.expect(
        "known characteristics are labelled",
        len(labelled) >= 6,
        f"only {len(labelled)} labelled",
    )

    unreadable = [
        characteristic
        for service in dump.services
        for characteristic in service.characteristics
        if characteristic.value is not None and characteristic.value.error
    ]
    report.equal("a readable characteristic that fails is recorded", unreadable, [])

    write_only = [
        characteristic
        for service in dump.services
        for characteristic in service.characteristics
        if characteristic.uuid == diag.COMMAND_UUID
    ][0]
    report.expect("a write-only characteristic is not read", write_only.value is None)

    descriptor = [
        descriptor
        for service in dump.services
        for characteristic in service.characteristics
        for descriptor in characteristic.descriptors
    ]
    report.equal("descriptors are enumerated", len(descriptor), 1)
    report.equal("descriptor values are read", descriptor[0].value.hex, "01 00")


async def check_mtu_reporting(report: Report) -> None:
    plain = FakeWindow()
    report.equal(
        "a backend with no MTU hook still reports its size",
        await diag.negotiated_mtu(plain),
        128,
    )

    class BlueZish:
        """A backend that only reports the real MTU once it is acquired."""

        def __init__(self) -> None:
            self.acquired = False

        async def _acquire_mtu(self) -> None:
            self.acquired = True

    class Nudgeable:
        def __init__(self) -> None:
            self._backend = BlueZish()

        @property
        def mtu_size(self) -> int:
            return 128 if self._backend.acquired else 23

    nudgeable = Nudgeable()
    report.equal(
        "a BlueZ backend is nudged past the 23-byte floor",
        await diag.negotiated_mtu(nudgeable),
        128,
    )

    class Broken:
        _backend = object()
        mtu_size = 64

    report.equal(
        "a backend that raises is tolerated", await diag.negotiated_mtu(Broken()), 64
    )


async def check_dump_of_led_less_window(report: Report) -> None:
    settings = {k: v for k, v in WORKING_SETTINGS.items() if k != "LedBrightness"}
    window = FakeWindow(settings)
    dump = await diag.collect_dump(window, diag.Target(address="AA:BB:CC:DD:EE:00"))
    await window.drain()
    report.equal("a missing led key is diagnosed", dump.led.verdict, "key-absent")
    absent = [s for s in dump.settings if s.kind == "absent"]
    report.equal("the absent key is listed", [s.key for s in absent], ["LedBrightness"])


async def check_unreadable_settings_need_provoking(report: Report) -> None:
    window = FakeWindow(readable_settings=False)
    dump = await diag.collect_dump(window, diag.Target(address="AA:BB:CC:DD:EE:01"))
    await window.drain()
    report.equal(
        "an unreadable document leaves the question open",
        dump.led.verdict,
        "no-document",
    )

    window = FakeWindow(readable_settings=False)
    dump = await diag.collect_dump(
        window,
        diag.Target(address="AA:BB:CC:DD:EE:01"),
        provoke=True,
        echo_timeout=1.0,
    )
    await window.drain()
    report.equal(
        "provoking recovers the document", dump.quick_settings_source, ["notify"]
    )
    report.equal(
        "the provoked document answers the led question", dump.led.verdict, "range-on"
    )
    report.equal("provoking sends the app's C token", window.writes[0][1], b"C")
    report.expect(
        "reassembly across notifications works",
        dump.notifications >= 1,
        f"{dump.notifications} documents",
    )


async def check_write_echo_paths(report: Report) -> None:
    window = FakeWindow()
    dump = await diag.collect_dump(
        window,
        diag.Target(address="AA:BB:CC:DD:EE:02"),
        writes=[("LedBrightness", 9)],
        echo_timeout=1.0,
    )
    await window.drain()
    test = dump.writes[0]
    report.equal("a matched echo is recognised", test.verdict, "echo-matched")
    report.equal("the echoed value is recorded", test.echoed_value, 9)
    report.equal("the previous value is recorded", test.previous, 7)
    report.expect("the previous value is restored", test.restored)
    report.equal(
        "the restore writes the old value",
        json.loads(window.writes[-1][1].decode()),
        {"LedBrightness": 7},
    )

    window = FakeWindow(echo="clamp", clamp_to=0)
    dump = await diag.collect_dump(
        window,
        diag.Target(address="AA:BB:CC:DD:EE:03"),
        writes=[("LedBrightness", 9)],
        echo_timeout=1.0,
    )
    await window.drain()
    report.equal("a clamped echo is recognised", dump.writes[0].verdict, "echo-clamped")

    window = FakeWindow(echo="none")
    dump = await diag.collect_dump(
        window,
        diag.Target(address="AA:BB:CC:DD:EE:04"),
        writes=[("LedBrightness", 9)],
        echo_timeout=0.2,
    )
    await window.drain()
    report.equal("a silent window is recognised", dump.writes[0].verdict, "no-echo")

    window = FakeWindow(echo="reject")
    dump = await diag.collect_dump(
        window,
        diag.Target(address="AA:BB:CC:DD:EE:05"),
        writes=[("LedBrightness", 9)],
        echo_timeout=0.2,
    )
    await window.drain()
    report.equal(
        "a refused write is recognised", dump.writes[0].verdict, "write-rejected"
    )
    report.expect("a refused write records the error", bool(dump.writes[0].error))


async def check_normalized_reports_diff_clean(report: Report) -> None:
    first = await diag.collect_dump(
        FakeWindow(), diag.Target(address="AA:BB:CC:DD:EE:06", name="Studio", rssi=-58)
    )
    second_window = FakeWindow()
    second_window.services[0].characteristics[
        0
    ].value = b"11112222-3333-4444-5555-666677778888,Bedroom"
    second = await diag.collect_dump(
        second_window,
        diag.Target(address="11:22:33:44:55:66", name="Bedroom", rssi=-71),
    )

    raw_differs = diag.format_dump(first) != diag.format_dump(second)
    report.expect("raw reports differ between units", raw_differs)

    left = diag.format_dump(diag.normalize(first))
    right = diag.format_dump(diag.normalize(second))
    report.equal("normalized reports of identical units match", left, right)
    report.expect(
        "normalization masks the address", diag.MASK in left, "no mask in report"
    )
    for leaked in ("AA:BB:CC:DD:EE:06", "0f8c1d3a", "Studio", "-58 dBm"):
        report.expect(
            f"normalization removes {leaked}",
            leaked not in left,
            "still present",
        )

    report.expect(
        "the report warns before sharing",
        "WARNING" in diag.report_header() and "public" in diag.report_header(),
    )
    report.equal(
        "the filename stem uses the stable identity", diag.slug(first)[:8], "0f8c1d3a"
    )
    report.expect(
        "two units get two filenames",
        diag.slug(first) != diag.slug(second),
        f"both {diag.slug(first)}",
    )
    # A masked dump has no identity left, so --out has to name the file from
    # the unmasked one or every normalized dump collides on one path.
    report.equal(
        "a masked dump has no identity to name a file after",
        diag.slug(diag.normalize(first)),
        "window",
    )


def check_command_line(report: Report) -> None:
    parser = diag.build_parser()
    cases: list[list[str]] = [
        ["scan"],
        ["scan", "--unfiltered"],
        ["--json", "--normalize", "--adapter", "hci0", "--scan-timeout", "3", "scan"],
        ["dump"],
        ["dump", "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"],
        ["dump", "--name", "Studio", "--max", "2", "--timeout", "5"],
        ["dump", "--provoke", "--echo-timeout", "2", "--no-descriptors"],
        ["dump", "--led-write", "5", "--no-restore"],
        ["dump", "--write", "ScreenBrightness=6", "--write", "SoundOnly=false"],
        ["--json", "dump", "--out", "/tmp/dumps"],
    ]
    for argv in cases:
        try:
            parser.parse_args(argv)
        except SystemExit:
            report.expect(f"parses {' '.join(argv)}", False, "argparse rejected it")
        else:
            report.passed += 1

    report.equal(
        "a level write is typed as an integer",
        diag.parse_write_argument("LedBrightness=4"),
        ("LedBrightness", 4),
    )
    report.equal(
        "a boolean write is typed as a boolean",
        diag.parse_write_argument("SoundOnly=true"),
        ("SoundOnly", True),
    )
    for bad in ("LedBrightness", "NotASetting=1"):
        try:
            diag.parse_write_argument(bad)
        except Exception:
            report.passed += 1
        else:
            report.expect(f"rejects {bad}", False, "accepted")


class FakeAdbd:
    """A listener that answers the ADB handshake the way a device would."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.server: asyncio.Server | None = None
        self.port = 0
        self.request = b""

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            self.request = await reader.readexactly(24)
            length = struct.unpack_from("<I", self.request, 12)[0]
            if length:
                await reader.readexactly(length)
            if self.reply == "cnxn":
                banner = b"device::ro.product.name=aw102;features=cmd,shell_v2\x00"
                writer.write(
                    netscan._adb_message(netscan.ADB_CNXN, 0x01000001, 4096, banner)
                )
            elif self.reply == "auth":
                writer.write(netscan._adb_message(netscan.ADB_AUTH, 1, 0, b"\x11" * 20))
            elif self.reply == "garbage":
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\npadding-to-24-bytes")
            elif self.reply == "silent":
                await reader.read()
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()


def check_adb_framing(report: Report) -> None:
    framed = netscan._adb_message(netscan.ADB_CNXN, 1, 2, b"host::")
    command, arg0, arg1, length, check, magic = struct.unpack_from("<6I", framed, 0)
    report.equal("the command word is CNXN", command, netscan.ADB_CNXN)
    report.equal("the arguments survive framing", (arg0, arg1), (1, 2))
    report.equal("the payload length is set", length, 6)
    report.equal("the checksum is a byte sum", check, sum(b"host::"))
    report.equal("the magic word is the inverted command", magic, command ^ 0xFFFFFFFF)
    report.equal("the payload follows the header", framed[24:], b"host::")


async def check_adb_probe(report: Report) -> None:
    expectations = {
        "cnxn": "adb-open",
        "auth": "adb-auth-required",
        "garbage": "not-adb",
        "silent": "no-reply",
    }
    for reply, expected in expectations.items():
        server = FakeAdbd(reply)
        await server.start()
        try:
            result = await netscan.adb_probe("127.0.0.1", server.port, 0.4)
        finally:
            await server.stop()
        report.equal(f"a {reply} reply reads as {expected}", result["state"], expected)
        if reply == "cnxn":
            report.expect(
                "the device banner is captured",
                "ro.product.name" in result.get("banner", ""),
                f"got {result.get('banner')!r}",
            )
        report.equal(
            f"a {reply} listener is sent a real handshake",
            struct.unpack_from("<I", server.request, 0)[0] if server.request else 0,
            netscan.ADB_CNXN,
        )

    closed = await netscan.adb_probe("127.0.0.1", 1, 0.3)
    report.equal("a refused port reads as unreachable", closed["state"], "unreachable")


async def check_android_surfaces(report: Report) -> None:
    server = FakeAdbd("cnxn")
    await server.start()
    try:
        surfaces = await netscan.android_surfaces(
            "127.0.0.1", ((server.port, "fake adbd"), (1, "refused")), 0.4, 8
        )
    finally:
        await server.stop()
    found = {surface.port: surface for surface in surfaces}
    report.equal("an open port is reported open", found[server.port].state, "open")
    report.equal("a refused port is reported closed", found[1].state, "closed")
    report.equal(
        "adbd on a non-default port is still found",
        found[server.port].adb,
        "adb-open",
    )
    report.expect(
        "its banner is reported",
        "ro.product.name" in (found[server.port].detail or ""),
        f"got {found[server.port].detail!r}",
    )
    report.expect(
        "an open port carries a next step", bool(found[server.port].next_step)
    )
    report.expect("a closed port carries no next step", found[1].next_step is None)

    server = FakeAdbd("garbage")
    await server.start()
    try:
        surfaces = await netscan.android_surfaces(
            "127.0.0.1", ((server.port, "not adbd"),), 0.4, 8
        )
    finally:
        await server.stop()
    report.expect(
        "a non-ADB listener on a non-ADB port is not labelled",
        surfaces[0].adb is None,
        f"got {surfaces[0].adb!r}",
    )

    report.expect(
        "adb gets the logcat and dumpsys instruction",
        "logcat" in (netscan._next_step("192.0.2.10", 5555, "adb-open") or ""),
    )
    report.expect(
        "an auth-required adb mentions the on-screen prompt",
        "prompt" in (netscan._next_step("192.0.2.10", 5555, "adb-auth-required") or ""),
    )
    report.expect(
        "a non-ADB listener on 5555 is not mistaken for adb",
        "does not speak ADB" in (netscan._next_step("192.0.2.10", 5555, None) or ""),
    )
    report.expect(
        "the kiosk admin port gets a URL",
        "2323" in (netscan._next_step("192.0.2.10", 2323, None) or ""),
    )
    report.equal(
        "any other open port gets a fingerprint suggestion",
        netscan._next_step("192.0.2.10", 9999, None),
        "fingerprint 192.0.2.10 9999",
    )
    report.expect(
        "5555 is in the built-in port set",
        5555 in {port for port, _ in netscan.ANDROID_PORTS},
    )


async def run() -> int:
    report = Report()
    check_value_rendering(report)
    check_settings_description(report)
    check_led_reasoning(report)
    check_command_line(report)
    check_adb_framing(report)
    await check_dump_of_working_window(report)
    await check_mtu_reporting(report)
    await check_dump_of_led_less_window(report)
    await check_unreadable_settings_need_provoking(report)
    await check_write_echo_paths(report)
    await check_normalized_reports_diff_clean(report)
    await check_adb_probe(report)
    await check_android_surfaces(report)

    if report.failures:
        print(f"{len(report.failures)} failed, {report.passed} passed")
        for failure in report.failures:
            print(f"  FAIL {failure}")
        return 1
    print(f"{report.passed} expectations passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
