"""Number entities for Atmoph Window quick settings."""

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AtmophCoordinator
from .entity import AtmophEntity
from .protocol import Level


@dataclass(frozen=True, kw_only=True)
class AtmophNumberDescription(NumberEntityDescription):
    """Describe a numeric quick setting."""

    setting_key: str


NUMBERS = (
    AtmophNumberDescription(
        key="landscape_volume",
        translation_key="landscape_volume",
        setting_key="LandscapeVolumeLevel",
        icon="mdi:volume-high",
    ),
    AtmophNumberDescription(
        key="soundscape_volume",
        translation_key="soundscape_volume",
        setting_key="SoundscapeVolumeLevel",
        icon="mdi:music-note",
    ),
    AtmophNumberDescription(
        key="screen_brightness",
        translation_key="screen_brightness",
        setting_key="ScreenBrightness",
        icon="mdi:brightness-6",
    ),
    AtmophNumberDescription(
        key="led_brightness",
        translation_key="led_brightness",
        setting_key="LedBrightness",
        icon="mdi:led-on",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up number entities."""
    coordinator: AtmophCoordinator = entry.runtime_data
    async_add_entities(
        AtmophNumber(coordinator, description) for description in NUMBERS
    )


class AtmophNumber(AtmophEntity, NumberEntity):
    """A bounded numeric setting reported by the window."""

    _attr_mode = NumberMode.SLIDER
    _attr_native_step = 1
    entity_description: AtmophNumberDescription

    def __init__(
        self, coordinator: AtmophCoordinator, description: AtmophNumberDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def _level(self) -> Level | None:
        return Level.from_wire(
            self.coordinator.data.quick_settings.get(
                self.entity_description.setting_key
            )
        )

    @property
    def available(self) -> bool:
        """Return whether the setting has been reported."""
        return super().available and self._level is not None

    @property
    def native_value(self) -> float | None:
        """Return the reported value."""
        level = self._level
        return level.value if level is not None else None

    @property
    def native_min_value(self) -> float:
        """Return the device-reported lower bound."""
        level = self._level
        return level.minimum if level is not None else 0

    @property
    def native_max_value(self) -> float:
        """Return the device-reported upper bound."""
        level = self._level
        return level.maximum if level is not None else 100

    async def async_set_native_value(self, value: float) -> None:
        """Write a numeric setting."""
        level = self._level
        integer = round(value)
        if level is None or not level.minimum <= integer <= level.maximum:
            raise ValueError(f"Value {integer} is outside the window's reported range")
        await self.coordinator.async_set_setting(
            self.entity_description.setting_key, integer
        )
