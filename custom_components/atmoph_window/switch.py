"""Switch entities for Atmoph Window."""

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AtmophCoordinator
from .entity import AtmophEntity


@dataclass(frozen=True, kw_only=True)
class AtmophSettingSwitchDescription(SwitchEntityDescription):
    """Describe a boolean quick setting."""

    setting_key: str


SETTING_SWITCHES = (
    AtmophSettingSwitchDescription(
        key="widgets_visible",
        translation_key="widgets_visible",
        setting_key="WidgetsVisible",
    ),
    AtmophSettingSwitchDescription(
        key="daily_routine",
        translation_key="daily_routine",
        setting_key="DailyRoutineEnable",
    ),
    AtmophSettingSwitchDescription(
        key="sound_only",
        translation_key="sound_only",
        setting_key="SoundOnly",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    coordinator: AtmophCoordinator = entry.runtime_data
    async_add_entities(
        [
            AtmophDisplaySwitch(coordinator),
            *(
                AtmophSettingSwitch(coordinator, description)
                for description in SETTING_SWITCHES
            ),
        ]
    )


class AtmophDisplaySwitch(AtmophEntity, SwitchEntity):
    """Idempotent display power control."""

    _attr_translation_key = "display"

    def __init__(self, coordinator: AtmophCoordinator) -> None:
        super().__init__(coordinator, "display")

    @property
    def is_on(self) -> bool | None:
        """Return the display's reported state."""
        return self.coordinator.data.power

    async def async_turn_on(self, **kwargs: object) -> None:
        """Wake the display."""
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Put the display to sleep."""
        await self.coordinator.async_set_power(False)


class AtmophSettingSwitch(AtmophEntity, SwitchEntity):
    """Boolean quick-menu setting."""

    entity_description: AtmophSettingSwitchDescription

    def __init__(
        self,
        coordinator: AtmophCoordinator,
        description: AtmophSettingSwitchDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the reported setting."""
        value = self.coordinator.data.quick_settings.get(
            self.entity_description.setting_key
        )
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: object) -> None:
        """Enable the setting."""
        await self.coordinator.async_set_setting(
            self.entity_description.setting_key, True
        )

    async def async_turn_off(self, **kwargs: object) -> None:
        """Disable the setting."""
        await self.coordinator.async_set_setting(
            self.entity_description.setting_key, False
        )
