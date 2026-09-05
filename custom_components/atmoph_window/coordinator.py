"""Connection lifecycle for Atmoph Window."""

from __future__ import annotations

import contextlib
import logging
from datetime import timedelta
from typing import Any

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AtmophClient
from .const import (
    CONF_ADVERTISED_NAME,
    CONF_DEVICE_UUID,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .identity import async_device_key
from .protocol import SERVICE_UUID, AtmophState

_LOGGER = logging.getLogger(__name__)

type AtmophConfigEntry = ConfigEntry[AtmophCoordinator]


class AtmophCoordinator(DataUpdateCoordinator[AtmophState]):
    """Resolve rotating addresses and maintain one BLE connection."""

    config_entry: AtmophConfigEntry

    def __init__(self, hass: HomeAssistant, entry: AtmophConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}-{entry.entry_id}",
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL),
        )
        self.advertised_name = entry.data[CONF_ADVERTISED_NAME]
        self._last_address: str | None = entry.data.get("address")
        self._bleak: Any | None = None
        self._client: AtmophClient | None = None

    @property
    def device_key(self) -> str:
        """Return the stable key entities and the device registry are built on."""
        return async_device_key(self.config_entry)

    @callback
    def async_handle_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        """Follow the stable advertised name across address rotations."""
        del change
        if service_info.name != self.advertised_name:
            return
        self._last_address = service_info.address
        if self._client is None or not self._client.is_connected:
            self.config_entry.async_create_task(self.hass, self.async_request_refresh())

    async def _async_update_data(self) -> AtmophState:
        try:
            client = await self._async_ensure_client()
            return await client.refresh()
        except Exception as err:
            await self._async_disconnect()
            raise UpdateFailed(
                f"Unable to update {self.advertised_name}: {err}"
            ) from err

    async def async_send_command(self, command: str) -> None:
        """Send a command and publish refreshed state."""
        client = await self._async_ensure_client()
        await client.send_command(command)
        self.async_set_updated_data(client.state)

    async def async_set_power(self, desired: bool) -> None:
        """Set display power and wait for confirmation."""
        client = await self._async_ensure_client()
        await client.set_power(desired)
        self.async_set_updated_data(client.state)

    async def async_set_setting(self, key: str, value: bool | int | str) -> None:
        """Write a quick setting."""
        client = await self._async_ensure_client()
        await client.set_setting(key, value)
        client.state.apply_setting_write(key, value)
        self.async_set_updated_data(client.state)

    async def async_shutdown(self) -> None:
        """Cancel scheduled refreshes and release the BLE connection."""
        await super().async_shutdown()
        await self._async_disconnect()

    async def _async_ensure_client(self) -> AtmophClient:
        if self._client is not None and self._client.is_connected:
            return self._client

        device = self._resolve_device()
        if device is None:
            raise RuntimeError("No connectable advertisement is currently available")

        self._last_address = device.address
        self._bleak = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.advertised_name,
            disconnected_callback=self._disconnected,
            max_attempts=3,
        )
        # State is not published until the window has proved it is the one this
        # entry was set up for, so a wrong window cannot write its view or its
        # power into this entry's entities on the way to being rejected.
        client = AtmophClient(self._bleak)
        try:
            await client.initialize(self.config_entry.data.get(CONF_DEVICE_UUID))
        except Exception:
            await self._async_disconnect()
            raise
        client.set_update_callback(self._handle_state)
        self._client = client
        return client

    def _resolve_device(self) -> BLEDevice | None:
        candidates = [
            info
            for info in bluetooth.async_discovered_service_info(
                self.hass, connectable=True
            )
            if info.name == self.advertised_name
            and SERVICE_UUID in {uuid.lower() for uuid in info.service_uuids}
        ]
        if candidates:
            return max(candidates, key=lambda info: info.rssi).device
        if self._last_address:
            return bluetooth.async_ble_device_from_address(
                self.hass, self._last_address, connectable=True
            )
        return None

    # Bleak delivers notifications and the disconnect callback from whichever
    # thread its backend runs on, and Home Assistant refuses to write entity
    # state off the event loop. Both hops therefore go through the loop.
    def _handle_state(self, state: AtmophState) -> None:
        self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, state)

    def _disconnected(self, client: Any) -> None:
        del client
        self.hass.loop.call_soon_threadsafe(self._mark_disconnected)

    @callback
    def _mark_disconnected(self) -> None:
        self._bleak = None
        self._client = None

    async def _async_disconnect(self) -> None:
        client, bleak = self._client, self._bleak
        self._client = None
        self._bleak = None
        if client is not None:
            await client.close()
        # This runs while reporting an update failure, so a disconnect that
        # itself raises must not replace the error the caller is raising.
        with contextlib.suppress(Exception):
            if bleak is not None and bleak.is_connected:
                await bleak.disconnect()
