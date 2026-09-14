"""Connection lifecycle for Atmoph Window."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AtmophClient, WrongWindowError
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

    @contextlib.asynccontextmanager
    async def _reporting_failures(self) -> AsyncIterator[None]:
        """Turn transport and protocol failures into something a user can read.

        Everything wrapped here is reached from a service call, a button or a
        switch, where an unconverted exception is a traceback in the log and
        an opaque failure in the interface. The cause is chained, so the
        detail is still in the log for whoever wants it.
        """
        try:
            yield
        except HomeAssistantError:
            raise
        except WrongWindowError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="wrong_window",
                translation_placeholders={"name": self.advertised_name},
            ) from err
        except TimeoutError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="power_not_confirmed",
                translation_placeholders={"name": self.advertised_name},
            ) from err
        except Exception as err:
            raise self._unreachable() from err

    def _unreachable(self) -> HomeAssistantError:
        """Return the error for a window that will not answer."""
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="not_reachable",
            translation_placeholders={"name": self.advertised_name},
        )

    async def async_send_command(self, command: str) -> None:
        """Send a command and publish refreshed state."""
        async with self._reporting_failures():
            client = await self._async_ensure_client()
            await client.send_command(command)
            self.async_set_updated_data(client.state)

    async def async_set_power(self, desired: bool) -> None:
        """Set display power and wait for confirmation."""
        async with self._reporting_failures():
            client = await self._async_ensure_client()
            await client.set_power(desired)
            self.async_set_updated_data(client.state)

    @callback
    def async_describe_gatt(self) -> list[dict[str, Any]]:
        """Describe the connected window's GATT table, or nothing if down."""
        if self._client is None or not self._client.is_connected:
            return []
        return self._client.describe_gatt()

    async def async_reported_setting(self, key: str) -> object | None:
        """Return the window's own report of one quick setting.

        The window announces the whole settings object on connect, so a key
        missing from it is either firmware that does not implement it or a
        notification that went astray. Re-reading the characteristic separates
        the two, which matters because the write format carries no type: the
        report is the only thing that says whether a key takes a boolean or a
        bounded integer.
        """
        reported = self.data.quick_settings.get(key)
        if reported is not None:
            return reported

        async with self._reporting_failures():
            client = await self._async_ensure_client()
            state = await client.refresh()
            self.async_set_updated_data(state)
            return state.quick_settings.get(key)

    async def async_set_setting(self, key: str, value: bool | int | str) -> None:
        """Write a quick setting."""
        async with self._reporting_failures():
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
            raise self._unreachable()

        # Release whatever came before rather than overwriting the reference.
        # A dropped client keeps its notify subscriptions and its disconnect
        # callback armed, so leaking one leaves a live link nothing will close.
        await self._async_disconnect()

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
        # A copy, because this connection has not proved which window it
        # reached: an unverified one must not write into the state Home
        # Assistant is already publishing.
        client = AtmophClient(
            self._bleak, state=self.data.copy() if self.data is not None else None
        )
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
        if self._last_address and (
            device := bluetooth.async_ble_device_from_address(
                self.hass, self._last_address, connectable=True
            )
        ):
            return device

        # The name rides in the scan response, so a window can be visible and
        # nameless for a long stretch - and after an address rotation the
        # remembered address is the one that rotated away, leaving nothing to
        # try. Any window advertising the vendor service is worth attempting,
        # because identity is confirmed after connecting and a wrong one is
        # refused before anything is written to it.
        nameless = [
            info
            for info in bluetooth.async_discovered_service_info(
                self.hass, connectable=True
            )
            if not info.name
            and SERVICE_UUID in {uuid.lower() for uuid in info.service_uuids}
        ]
        if nameless:
            return max(nameless, key=lambda info: info.rssi).device
        return None

    # Bleak delivers notifications and the disconnect callback from whichever
    # thread its backend runs on, and Home Assistant refuses to write entity
    # state off the event loop. Both hops therefore go through the loop.
    def _handle_state(self, state: AtmophState) -> None:
        # Config entries are not unloaded when Home Assistant stops, so this
        # can arrive from a bleak thread after the loop has closed. There is
        # nothing to deliver to at that point, and raising here surfaces as
        # noise from a thread nobody is watching.
        with contextlib.suppress(RuntimeError):
            self.hass.loop.call_soon_threadsafe(self._publish_pushed_state, state)

    @callback
    def _publish_pushed_state(self, state: AtmophState) -> None:
        """Publish pushed state without deferring the scheduled read.

        `async_set_updated_data` reschedules the refresh, so a window that
        notifies steadily would keep postponing it - and with the daily
        routine on, a window changes view by itself indefinitely. The poll is
        the only thing that re-reads the setting bounds and retries the
        characteristics a window may not have answered before, so it has to
        keep its own cadence rather than being pushed back by good news.
        """
        self.data = state
        self.last_update_success = True
        self.async_update_listeners()

    def _disconnected(self, client: Any) -> None:
        with contextlib.suppress(RuntimeError):
            self.hass.loop.call_soon_threadsafe(self._mark_disconnected, client)

    @callback
    def _mark_disconnected(self, client: Any = None) -> None:
        # Addresses rotate every few tens of seconds, so a disconnect callback
        # arriving after its replacement is already up is ordinary here. Acting
        # on it would take down a healthy connection and report the entry
        # unavailable while the new link was still open.
        if client is not None and client is not self._bleak:
            return

        self._bleak = None
        self._client = None
        # Entities would otherwise keep serving the last state they were told,
        # for up to a whole update interval. The display is a toggle, so an
        # automation reading a stale value does not merely display something
        # wrong - it inverts the command it then sends. Better unavailable.
        # `_shutdown_requested` is private to DataUpdateCoordinator and has no
        # public equivalent, so this is the line to look at first if a Home
        # Assistant upgrade starts reporting an error on every clean unload.
        if not self._shutdown_requested:
            self.async_set_update_error(
                UpdateFailed(f"{self.advertised_name} disconnected")
            )

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
