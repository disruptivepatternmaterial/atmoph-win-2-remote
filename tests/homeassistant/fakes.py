"""Stand-ins for a real window and Home Assistant's Bluetooth stack."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from bleak.backends.device import BLEDevice
from habluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant

from custom_components.atmoph_window.protocol import (
    COMMAND_UUID,
    IDENTITY_UUID,
    PANORAMA_ROLE_UUID,
    POWER_UUID,
    QUICK_SETTINGS_UUID,
    SERVICE_UUID,
    VIEW_ID_UUID,
    VIEW_IMAGE_UUID,
    VIEW_LOCATION_UUID,
    VIEW_TITLE_UUID,
)

WINDOW_NAME = "Living Room Window"
WINDOW_ADDRESS = "AA:BB:CC:DD:EE:FF"
ROTATED_ADDRESS = "11:22:33:44:55:66"
SECOND_WINDOW_NAME = "Bedroom Window"
SECOND_WINDOW_ADDRESS = "99:88:77:66:55:44"
VIEW_ID = "LAT2_IUOV6NFQ"
VIEW_REVISION = "7206c70d"


def make_service_info(
    name: str = WINDOW_NAME,
    address: str = WINDOW_ADDRESS,
    rssi: int = -60,
    service_uuids: list[str] | None = None,
) -> BluetoothServiceInfoBleak:
    """Build an advertisement in the shape Home Assistant reports it."""
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=rssi,
        manufacturer_data={},
        service_data={},
        service_uuids=[SERVICE_UUID] if service_uuids is None else service_uuids,
        source="local",
        device=BLEDevice(address, name, {}),
        advertisement=None,
        connectable=True,
        time=0,
        tx_power=-127,
    )


class FakeBleakClient:
    """Minimal in-memory GATT peripheral standing in for a window."""

    def __init__(
        self,
        address: str = WINDOW_ADDRESS,
        power: bool = True,
        view_id: bool = True,
    ) -> None:
        self.address = address
        self.connected = True
        self.values: dict[str, bytes] = {
            # Two configured windows have to look like two devices, and the
            # device UUID is what the integration keys the device registry on.
            IDENTITY_UUID: f"device-uuid-{address},Living Room".encode(),
            PANORAMA_ROLE_UUID: b"N",
            VIEW_TITLE_UUID: b"Kyoto",
            VIEW_IMAGE_UUID: b"https://example.invalid/view.jpg",
            VIEW_LOCATION_UUID: b"Kyoto, Japan",
            POWER_UUID: b"true" if power else b"false",
            QUICK_SETTINGS_UUID: json.dumps(
                {
                    "ScreenBrightness": {"min": 1, "max": 10, "value": 6},
                    "CurrentDecoration": {"min": 0, "max": 19, "value": 3},
                    "SoundscapeLayer": {"min": 0, "max": 5, "value": 2},
                    "WidgetsVisible": True,
                }
            ).encode(),
        }
        # No window is confirmed to implement the view-id characteristic, so
        # the fake can be built either way.
        if view_id:
            self.values[VIEW_ID_UUID] = f"{VIEW_ID}/{VIEW_REVISION}".encode()
        self.writes: list[tuple[str, bytes]] = []
        self.notifications: dict[str, Callable[[Any, bytearray], None]] = {}

    @property
    def is_connected(self) -> bool:
        """Return whether the fake transport is up."""
        return self.connected

    @property
    def commands(self) -> list[bytes]:
        """Return every remote-control command written so far."""
        return [data for uuid, data in self.writes if uuid == COMMAND_UUID]

    @property
    def settings_writes(self) -> list[bytes]:
        """Return every quick-setting document written so far."""
        return [data for uuid, data in self.writes if uuid == QUICK_SETTINGS_UUID]

    async def read_gatt_char(self, char_specifier: str) -> bytearray:
        """Return the stored characteristic value.

        A characteristic the window does not implement is missing rather than
        empty, which is what a real read of one raises on.
        """
        return bytearray(self.values[char_specifier])

    async def write_gatt_char(
        self, char_specifier: str, data: bytes, response: bool = True
    ) -> None:
        """Record a write and apply the toggle a real window would perform."""
        del response
        self.writes.append((char_specifier, data))
        if char_specifier == COMMAND_UUID and data == b"S":
            self.values[POWER_UUID] = (
                b"false" if self.values[POWER_UUID] == b"true" else b"true"
            )

    async def start_notify(
        self, char_specifier: str, callback: Callable[[Any, bytearray], None]
    ) -> None:
        """Record a notification subscription."""
        self.notifications[char_specifier] = callback

    async def stop_notify(self, char_specifier: str) -> None:
        """Drop a notification subscription."""
        self.notifications.pop(char_specifier, None)

    async def disconnect(self) -> None:
        """Mark the fake transport as down."""
        self.connected = False


class FakeBluetooth:
    """Stand-in for the Bluetooth integration's discovery and connect helpers."""

    def __init__(self) -> None:
        self.service_infos: list[BluetoothServiceInfoBleak] = [make_service_info()]
        self.clients: list[FakeBleakClient] = []
        self.callbacks: list[Callable[..., None]] = []
        self.unregister_calls = 0
        self.connect_failure: Exception | None = None
        self.view_id_supported = True

    @property
    def client(self) -> FakeBleakClient:
        """Return the most recently established connection."""
        return self.clients[-1]

    def client_at(self, address: str) -> FakeBleakClient:
        """Return the connection established to one particular window."""
        return next(
            client for client in reversed(self.clients) if client.address == address
        )

    def discovered_service_info(
        self, hass: HomeAssistant, connectable: bool = True
    ) -> list[BluetoothServiceInfoBleak]:
        """Return the advertisements currently in Home Assistant's cache."""
        del hass, connectable
        return self.service_infos

    def ble_device_from_address(
        self, hass: HomeAssistant, address: str, connectable: bool = True
    ) -> BLEDevice | None:
        """Resolve a remembered address against the cache."""
        del hass, connectable
        for info in self.service_infos:
            if info.address == address:
                return info.device
        return None

    def register_callback(
        self,
        hass: HomeAssistant,
        callback: Callable[..., None],
        matcher: Any,
        mode: Any,
    ) -> Callable[[], None]:
        """Register an advertisement callback and hand back an unregister."""
        del hass, matcher, mode
        self.callbacks.append(callback)

        def _unregister() -> None:
            self.unregister_calls += 1
            # Home Assistant's own unregister raises on a second call, so the
            # fake has to be just as unforgiving or the test proves nothing.
            self.callbacks.remove(callback)

        return _unregister

    async def establish_connection(
        self, client_class: type, device: BLEDevice, name: str, **kwargs: Any
    ) -> FakeBleakClient:
        """Return a fresh fake peripheral for the requested device."""
        del client_class, name, kwargs
        if self.connect_failure is not None:
            raise self.connect_failure
        self.clients.append(
            FakeBleakClient(address=device.address, view_id=self.view_id_supported)
        )
        return self.clients[-1]
