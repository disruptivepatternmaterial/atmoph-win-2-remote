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
        self._attr_unique_id = f"{coordinator.device_key}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the shared Home Assistant device.

        No `connections` entry: the only address a window has is a resolvable
        private one it rotates every few tens of seconds, so recording it would
        leave the registry holding an address that belongs to something else
        within the minute, and connections are matched across integrations.
        """
        state = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.device_key)},
            manufacturer="Atmoph",
            model="Window 2",
            name=(
                state.name
                if state is not None and state.name
                else self.coordinator.advertised_name
            ),
        )
