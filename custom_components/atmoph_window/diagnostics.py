"""Redacted diagnostics for Atmoph Window."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ADVERTISED_NAME
from .coordinator import AtmophCoordinator

_REDACT_ENTRY = {CONF_ADVERTISED_NAME, "address"}
_REDACT_STATE = {"device_uuid", "name", "view_image_url"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics without stable device identifiers."""
    coordinator: AtmophCoordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), _REDACT_ENTRY),
        "last_update_success": coordinator.last_update_success,
        "state": async_redact_data(asdict(coordinator.data), _REDACT_STATE),
    }
