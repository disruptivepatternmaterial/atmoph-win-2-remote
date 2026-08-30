"""Services for Atmoph Window.

The recovered protocol carries more than the entity platforms model. Two
control tokens map to no sensible entity, and two quick settings are levels
whose meaning is a catalogue index rather than a magnitude, so they were left
without one. These services keep all of it reachable from an automation
without inventing an entity per token.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_config_entry_ids

from .const import (
    ATTR_COMMAND,
    ATTR_SETTING,
    ATTR_VALUE,
    DOMAIN,
    SERVICE_SEND_COMMAND,
    SERVICE_SET_SETTING,
)
from .coordinator import AtmophCoordinator
from .protocol import COMMANDS, SETTING_KEYS, Level

# The token and key are validated in the handlers rather than with `vol.In`,
# so a mistake reaches the user as the integration's own translated message
# instead of a voluptuous type error.
SEND_COMMAND_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Required(ATTR_COMMAND): cv.string,
    }
)

SET_SETTING_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Required(ATTR_SETTING): cv.string,
        vol.Required(ATTR_VALUE): vol.Any(vol.Coerce(int), cv.boolean),
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services shared by every configured window."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        _async_send_command,
        schema=SEND_COMMAND_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SETTING,
        _async_set_setting,
        schema=SET_SETTING_SCHEMA,
    )


async def _async_send_command(call: ServiceCall) -> None:
    """Write one verified remote-control token to every targeted window."""
    command = call.data[ATTR_COMMAND]
    if command not in COMMANDS:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_command",
            translation_placeholders={
                "command": str(command),
                "commands": ", ".join(sorted(COMMANDS)),
            },
        )
    for coordinator in await _async_targeted_coordinators(call):
        await coordinator.async_send_command(command)


async def _async_set_setting(call: ServiceCall) -> None:
    """Write one quick setting to every targeted window."""
    setting = call.data[ATTR_SETTING]
    if setting not in SETTING_KEYS:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_setting",
            translation_placeholders={
                "setting": str(setting),
                "settings": ", ".join(sorted(SETTING_KEYS)),
            },
        )
    for coordinator in await _async_targeted_coordinators(call):
        value = _validated_value(coordinator, setting, call.data[ATTR_VALUE])
        await coordinator.async_set_setting(setting, value)


def _validated_value(
    coordinator: AtmophCoordinator, setting: str, value: bool | int
) -> bool | int:
    """Match a submitted value to the type and bounds the window reports.

    The write format is a bare value, so nothing in it distinguishes a boolean
    setting from a level. The window's own report of the setting decides, and a
    setting it has never reported is refused rather than guessed at.
    """
    reported = coordinator.data.quick_settings.get(setting)
    if isinstance(reported, bool):
        return bool(value)

    level = Level.from_wire(reported)
    if level is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="setting_not_reported",
        )

    integer = int(value)
    if not level.minimum <= integer <= level.maximum:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="value_out_of_range",
            translation_placeholders={
                "value": str(integer),
                "minimum": str(level.minimum),
                "maximum": str(level.maximum),
            },
        )
    return integer


async def _async_targeted_coordinators(call: ServiceCall) -> list[AtmophCoordinator]:
    """Resolve the loaded windows a call targets.

    More than one window can be configured, so a call has to name the ones it
    means through the usual entity, device, or area target.
    """
    coordinators = [
        entry.runtime_data
        for entry_id in await async_extract_config_entry_ids(call)
        if (entry := call.hass.config_entries.async_get_entry(entry_id)) is not None
        and entry.domain == DOMAIN
        and entry.state is ConfigEntryState.LOADED
    ]
    if not coordinators:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_window_targeted",
        )
    return coordinators
