"""Sensor entities for Atmoph Window."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_IMAGE_URL, ATTR_LOCATION, ATTR_PANORAMA_ROLE
from .coordinator import AtmophConfigEntry, AtmophCoordinator
from .entity import AtmophEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtmophConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Atmoph sensors."""
    coordinator = entry.runtime_data
    async_add_entities([AtmophCurrentViewSensor(coordinator)])


class AtmophCurrentViewSensor(AtmophEntity, SensorEntity):
    """Current scenery shown on the window."""

    _attr_translation_key = "current_view"

    def __init__(self, coordinator: AtmophCoordinator) -> None:
        super().__init__(coordinator, "current_view")

    @property
    def native_value(self) -> str | None:
        """Return the current view title."""
        return self.coordinator.data.view_title

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return metadata from the view characteristics."""
        state = self.coordinator.data
        return {
            key: value
            for key, value in {
                ATTR_LOCATION: state.view_location,
                ATTR_IMAGE_URL: state.view_image_url,
                ATTR_PANORAMA_ROLE: state.panorama_role,
            }.items()
            if value
        }
