"""Atmoph Window 2 integration."""

from homeassistant.components import bluetooth
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import AtmophConfigEntry, AtmophCoordinator
from .protocol import SERVICE_UUID

PLATFORMS = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: AtmophConfigEntry) -> bool:
    """Set up an Atmoph Window from a config entry."""
    coordinator = AtmophCoordinator(hass, entry)
    entry.runtime_data = coordinator

    # The first refresh raises ConfigEntryNotReady when the window is out of
    # range, so nothing may be registered before it: Home Assistant runs the
    # entry's unload callbacks itself when setup fails.
    await coordinator.async_config_entry_first_refresh()

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            coordinator.async_handle_advertisement,
            {"service_uuid": SERVICE_UUID, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AtmophConfigEntry) -> bool:
    """Unload an Atmoph Window config entry.

    The BLE connection is released by the coordinator's `async_shutdown`, which
    `DataUpdateCoordinator` registers on the entry for us.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
