"""Shared entity support for Atmoph Window."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AtmophCoordinator


class AtmophEntity(CoordinatorEntity[AtmophCoordinator]):
    """Base entity tied to one Atmoph coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AtmophCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.advertised_name}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the shared Home Assistant device."""
        state = self.coordinator.data
        identifier = (
            state.device_uuid
            if state is not None and state.device_uuid
            else self.coordinator.advertised_name
        )
        return DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            manufacturer="Atmoph",
            model="Window 2",
            name=(
                state.name
                if state is not None and state.name
                else self.coordinator.advertised_name
            ),
        )
