"""Remote-control buttons for Atmoph Window."""

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AtmophConfigEntry, AtmophCoordinator
from .entity import AtmophEntity

BUTTONS = (
    ButtonEntityDescription(key="previous_view", translation_key="previous_view"),
    ButtonEntityDescription(key="next_view", translation_key="next_view"),
    ButtonEntityDescription(key="menu", translation_key="menu"),
    ButtonEntityDescription(key="quick_menu", translation_key="quick_menu"),
    ButtonEntityDescription(key="views", translation_key="views"),
    ButtonEntityDescription(key="back", translation_key="back"),
    ButtonEntityDescription(key="up", translation_key="up"),
    ButtonEntityDescription(key="down", translation_key="down"),
    ButtonEntityDescription(key="left", translation_key="left"),
    ButtonEntityDescription(key="right", translation_key="right"),
    ButtonEntityDescription(key="tap", translation_key="tap"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtmophConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up remote buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        AtmophButton(coordinator, description) for description in BUTTONS
    )


class AtmophButton(AtmophEntity, ButtonEntity):
    """Send a command to the window's remote-control characteristic."""

    def __init__(
        self, coordinator: AtmophCoordinator, description: ButtonEntityDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        """Send the mapped command."""
        await self.coordinator.async_send_command(self.entity_description.key)
