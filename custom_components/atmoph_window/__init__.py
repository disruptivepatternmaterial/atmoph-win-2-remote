"""Atmoph Window 2 integration."""

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import AtmophCoordinator
from .protocol import SERVICE_UUID

PLATFORMS = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an Atmoph Window from a config entry."""
    coordinator = AtmophCoordinator(hass, entry)
    entry.runtime_data = coordinator

    unregister = bluetooth.async_register_callback(
        hass,
        coordinator.async_handle_advertisement,
        {"service_uuid": SERVICE_UUID, "connectable": True},
        bluetooth.BluetoothScanningMode.ACTIVE,
    )
    entry.async_on_unload(unregister)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        unregister()
        entry.runtime_data = None
        raise

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Atmoph Window config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    coordinator: AtmophCoordinator = entry.runtime_data
    await coordinator.async_shutdown()
    return True
