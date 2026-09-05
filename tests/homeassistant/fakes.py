"""Stand-ins for a real window and Home Assistant's Bluetooth stack."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from bleak.backends.device import BLEDevice
from habluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth import BluetoothChange, BluetoothScanningMode
from homeassistant.core import HomeAssistant

from custom_components.atmoph_window.protocol import (
    COMMAND_UUID,
    FOCUSING_VIEW_UUID,
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
from tests.window import (
    REPORTED_SETTINGS,
    DisplayPower,
    FakeCharacteristic,
    FakeClock,
    Toggle,
)

WINDOW_NAME = "Living Room Window"
WINDOW_ADDRESS = "AA:BB:CC:DD:EE:FF"
ROTATED_ADDRESS = "11:22:33:44:55:66"
SECOND_WINDOW_NAME = "Bedroom Window"
SECOND_WINDOW_ADDRESS = "99:88:77:66:55:44"
VIEW_ID = "LAT2_IUOV6NFQ"
VIEW_REVISION = "7206c70d"
VIEW_TITLE = "Kyoto"
VIEW_LOCATION = "Kyoto, Japan"
VIEW_IMAGE_URL = "https://example.invalid/view.jpg"


def device_uuid_for(name: str = WINDOW_NAME) -> str:
    """Return the stable device UUID a fake window reports.

    Keyed on the window rather than on its address. A real window keeps this
    value while its BLE address rotates every few tens of seconds - that is the
    whole reason identity is read over GATT - so deriving it from the address
    would model a device that cannot exist, and would let a reconnection after
    a rotation look like a different window.
    """
    return f"device-uuid-{name.lower().replace(' ', '-')}"


def make_service_info(
    name: str = WINDOW_NAME,
    address: str = WINDOW_ADDRESS,
    rssi: int = -60,
    service_uuids: list[str] | None = None,
) -> BluetoothServiceInfoBleak:
    """Build an advertisement in the shape Home Assistant reports it.

    An empty name is the nameless case, not a malformed one: the name rides in
    the scan response rather than the advertisement, so the same window is seen
    named and nameless seconds apart.
    """
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=rssi,
        manufacturer_data={},
        service_data={},
        service_uuids=[SERVICE_UUID] if service_uuids is None else service_uuids,
        source="local",
        device=BLEDevice(address, name or None, {}),
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
        device_uuid: bool = True,
        name: str = WINDOW_NAME,
        clock: FakeClock | None = None,
        unreported_settings: Iterable[str] = (),
    ) -> None:
        self.address = address
        self.name = name
        self.connected = True
        self.settings: dict[str, Any] = {
            key: value
            for key, value in REPORTED_SETTINGS.items()
            if key not in set(unreported_settings)
        }
        self.values: dict[str, bytes] = {
            # Two configured windows have to look like two devices, and the
            # device UUID is what the integration keys the device registry on.
            # A window answering with an empty first field reports no UUID at
            # all, which the integration has to survive.
            IDENTITY_UUID: (
                f"{device_uuid_for(name)},{name}".encode()
                if device_uuid
                else f",{name}".encode()
            ),
            PANORAMA_ROLE_UUID: b"N",
            VIEW_TITLE_UUID: VIEW_TITLE.encode(),
            VIEW_IMAGE_UUID: VIEW_IMAGE_URL.encode(),
            VIEW_LOCATION_UUID: VIEW_LOCATION.encode(),
            POWER_UUID: b"true" if power else b"false",
            QUICK_SETTINGS_UUID: json.dumps(self.settings).encode(),
        }
        # No window is confirmed to implement the view-id characteristic, so
        # the fake can be built either way.
        if view_id:
            self.values[VIEW_ID_UUID] = f"{VIEW_ID}/{VIEW_REVISION}".encode()
        self.writes: list[tuple[str, bytes]] = []
        self.notifications: dict[str, Callable[[Any, bytearray], None]] = {}
        self.pending_notifications: list[tuple[str, bytes]] = []
        self.display = DisplayPower(clock if clock is not None else FakeClock())
        self.disconnected_callback: Callable[[Any], None] | None = None

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

    @property
    def toggles(self) -> list[Toggle]:
        """Return every `S` write and whether the display acted on it."""
        return self.display.toggles

    async def read_gatt_char(self, char_specifier: str) -> bytearray:
        """Return the stored characteristic value.

        A characteristic the window does not implement is missing rather than
        empty, which is what a real read of one raises on.
        """
        return bytearray(self.values[char_specifier])

    async def write_gatt_char(
        self, char_specifier: str, data: bytes, response: bool = True
    ) -> None:
        """Record a write and apply what a real window would do with it."""
        del response
        # A write to the power characteristic is recorded and then discarded.
        # The window advertises write on it and ignores both directions, so
        # anything that relies on one has to fail.
        self.writes.append((char_specifier, data))
        if char_specifier == COMMAND_UUID and data == b"S" and self.display.toggle():
            self.values[POWER_UUID] = (
                b"false" if self.values[POWER_UUID] == b"true" else b"true"
            )
        elif char_specifier == QUICK_SETTINGS_UUID:
            self._apply_setting(data)

    def _apply_setting(self, data: bytes) -> None:
        """Store a written setting and queue the echo that confirms it.

        The window answers a write on the notify channel about a second and a
        half later, and that echo is the only reliable sign the write landed.
        It is not always what was written: a value outside the bounds the
        device holds now comes back clamped.
        """
        for key, value in json.loads(data).items():
            current = self.settings.get(key)
            if isinstance(current, dict):
                clamped = max(current["min"], min(current["max"], int(value)))
                self.settings[key] = {**current, "value": clamped}
            else:
                self.settings[key] = bool(value)
        document = json.dumps(self.settings).encode()
        self.values[QUICK_SETTINGS_UUID] = document
        self.pending_notifications.append((QUICK_SETTINGS_UUID, document))

    def narrow_bounds(self, key: str, maximum: int) -> None:
        """Lower a setting's ceiling without telling anyone.

        A window whose bounds moved since it last reported them is the only
        way a write inside the range Home Assistant knows about can still come
        back clamped.
        """
        self.settings[key] = {**self.settings[key], "max": maximum}
        self.values[QUICK_SETTINGS_UUID] = json.dumps(self.settings).encode()

    async def start_notify(
        self, char_specifier: str, callback: Callable[[Any, bytearray], None]
    ) -> None:
        """Record a notification subscription."""
        self.notifications[char_specifier] = callback

    async def stop_notify(self, char_specifier: str) -> None:
        """Drop a notification subscription."""
        self.notifications.pop(char_specifier, None)

    def notify(self, uuid: str, payload: bytes) -> None:
        """Deliver a notification the way bleak delivers one.

        Bleak passes the characteristic the notification came from, never its
        UUID, so a fake handing over a bare string would let the client read
        `sender` directly and still pass.
        """
        self.notifications[uuid](FakeCharacteristic(uuid), bytearray(payload))

    def deliver_pending(self) -> None:
        """Fire the notifications the window would have sent by now.

        Held rather than sent inline because the echo of a write arrives about
        a second and a half after it, which is what lets the coordinator's
        optimistic update land first and then be corrected.
        """
        pending, self.pending_notifications = self.pending_notifications, []
        for uuid, payload in pending:
            self.notify(uuid, payload)

    async def disconnect(self) -> None:
        """Drop the transport and tell whoever asked to be told.

        A window's address rotates out from under a live connection, so a
        disconnection the integration did not ask for is ordinary and arrives
        as this callback rather than as an error on the next operation.
        """
        self.connected = False
        if self.disconnected_callback is not None:
            self.disconnected_callback(self)


class FakeBluetooth:
    """Stand-in for the Bluetooth integration's discovery and connect helpers."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.service_infos: list[BluetoothServiceInfoBleak] = [make_service_info()]
        self.clients: list[FakeBleakClient] = []
        self.callbacks: list[tuple[Callable[..., None], BluetoothScanningMode]] = []
        self.unregister_calls = 0
        self.connect_failure: Exception | None = None
        self.view_id_supported = True
        self.device_uuid_reported = True
        self.unreported_settings: frozenset[str] = frozenset()
        self.clock = clock if clock is not None else FakeClock()

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
        mode: BluetoothScanningMode,
    ) -> Callable[[], None]:
        """Register an advertisement callback and hand back an unregister."""
        del hass, matcher
        registration = (callback, mode)
        self.callbacks.append(registration)

        def _unregister() -> None:
            self.unregister_calls += 1
            # Home Assistant's own unregister raises on a second call, so the
            # fake has to be just as unforgiving or the test proves nothing.
            self.callbacks.remove(registration)

        return _unregister

    def advertise(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Deliver an advertisement to every registered callback.

        The name rides in the scan response, so a passive scanner is handed
        the same packet with no name on it. Delivering the name whatever the
        mode would let the integration register for passive scanning, stop
        seeing names, and still pass every test.
        """
        for callback, mode in self.callbacks:
            delivered = (
                service_info
                if mode is BluetoothScanningMode.ACTIVE
                else make_service_info(
                    name="",
                    address=service_info.address,
                    rssi=service_info.rssi,
                    service_uuids=list(service_info.service_uuids),
                )
            )
            callback(delivered, BluetoothChange.ADVERTISEMENT)

    async def establish_connection(
        self, client_class: type, device: BLEDevice, name: str, **kwargs: Any
    ) -> FakeBleakClient:
        """Return a fresh fake peripheral for the requested device."""
        del client_class, name
        if self.connect_failure is not None:
            raise self.connect_failure
        client = FakeBleakClient(
            address=device.address,
            name=device.name or WINDOW_NAME,
            view_id=self.view_id_supported,
            device_uuid=self.device_uuid_reported,
            clock=self.clock,
            unreported_settings=self.unreported_settings,
        )
        client.disconnected_callback = kwargs.get("disconnected_callback")
        self.clients.append(client)
        return client


NOTIFYING_CHARACTERISTICS = (
    PANORAMA_ROLE_UUID,
    VIEW_TITLE_UUID,
    VIEW_IMAGE_UUID,
    VIEW_LOCATION_UUID,
    FOCUSING_VIEW_UUID,
    QUICK_SETTINGS_UUID,
    POWER_UUID,
    VIEW_ID_UUID,
)
