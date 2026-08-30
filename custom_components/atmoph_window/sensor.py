"""Sensor entities for Atmoph Window."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_IMAGE_URL,
    ATTR_LOCATION,
    ATTR_PANORAMA_ROLE,
    ATTR_REVISION,
)
from .coordinator import AtmophConfigEntry, AtmophCoordinator
from .entity import AtmophEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtmophConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Atmoph sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [AtmophCurrentViewSensor(coordinator)]
    # Setup only runs after a successful first refresh, so whether the window
    # answers the view-id characteristic is already known. A window that does
    # not implement it gets no entity rather than one stuck unavailable.
    if coordinator.data.view_id_supported:
        entities.append(AtmophViewIdSensor(coordinator))
    async_add_entities(entities)


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


class AtmophViewIdSensor(AtmophEntity, SensorEntity):
    """Machine-readable identity of the view the window is showing.

    View titles repeat across the Atmoph catalogue and change with the app's
    language, so matching on them in an automation is unreliable. This is the
    catalogue id instead. It is diagnostic because the characteristic behind it
    is a third-party hardware observation rather than something the Android app
    or this project has confirmed.
    """

    _attr_translation_key = "view_id"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AtmophCoordinator) -> None:
        super().__init__(coordinator, "view_id")

    @property
    def native_value(self) -> str | None:
        """Return the current view's catalogue id."""
        return self.coordinator.data.view_id

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return the render revision that completes the thumbnail path."""
        revision = self.coordinator.data.view_revision
        return {ATTR_REVISION: revision} if revision else {}
