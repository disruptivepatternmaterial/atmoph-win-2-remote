"""A fake window and a fake adbd for the diagnostics in tools/.

Neither tool can reach real hardware from a test run: Bluetooth is refused to
a sandboxed process, CI runners have no adapter, and LAN TCP connects are
dropped. So the parts that can be wrong on their own - value rendering, LED
reasoning, write-echo handling, report normalization, and the ADB handshake -
are checked against these stand-ins rather than against a device.

The fake window answers reads, echoes setting writes the way the firmware
does, and can be told to misbehave in each of the ways a real unit has been
seen to. The fake adbd is a real listening socket, so the handshake under test
goes over a real connection.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass, field
from typing import Any

import atmoph_diag as diag
import atmoph_netscan as netscan
from tests.window import REPORTED_SETTINGS

# The values a window with working LEDs reports. The min/max bounds are the
# hardware fact and live in tests/window.py, so only the values are set here.
# Two of them are load-bearing: LedBrightness has to start non-zero for a
# write test to have something to change and restore, and CurrentDecoration is
# named in the LED gate the report prints.
WORKING_VALUES: dict[str, int] = {
    "ScreenBrightness": 18,
    "LandscapeVolumeLevel": 12,
    "SoundscapeVolumeLevel": 4,
    "SoundscapeLayer": 1,
    "CurrentDecoration": 3,
    "LedBrightness": 7,
}


def _with_working_value(key: str, reported: object) -> object:
    """Override a level's value while keeping the bounds the hardware gave."""
    if not isinstance(reported, dict):
        return reported
    return {**reported, "value": WORKING_VALUES.get(key, reported["value"])}


WORKING_SETTINGS: dict[str, object] = {
    key: _with_working_value(key, reported)
    for key, reported in REPORTED_SETTINGS.items()
}

# The main service as docs/PROTOCOL.md maps it, trimmed to what the report
# needs to exercise: an identity value, a JSON setting, and a write-only entry.
# The UUIDs the protocol layer does not name are written out, because this is a
# record of what a window reported rather than a restatement of the constants.
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
