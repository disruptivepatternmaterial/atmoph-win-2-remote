"""Media player entity for Atmoph Window.

A window is a screen showing a moving landscape with its own sound, which is
close enough to Home Assistant's media player model to earn the built-in Media
Control card and voice-assistant support for free. Nothing here reads or writes
anything the other platforms do not: it is a second presentation of the same
state, which is why the switch, numbers and buttons all stay.
"""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import AtmophConfigEntry, AtmophCoordinator
from .entity import AtmophEntity
from .protocol import Level

VOLUME_SETTING = "LandscapeVolumeLevel"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtmophConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the media player."""
    async_add_entities([AtmophMediaPlayer(entry.runtime_data)])


class AtmophMediaPlayer(AtmophEntity, MediaPlayerEntity):
    """The current view, presented as something playing."""

    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_media_content_type = "video"
    # Deliberately no PLAY or PAUSE: the protocol has no such command, and
    # advertising one that silently does nothing is worse than omitting it.
    _BASE_FEATURES = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
    )
    _VOLUME_FEATURES = (
        MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_STEP
    )

    def __init__(self, coordinator: AtmophCoordinator) -> None:
        super().__init__(coordinator, "media_player")

    @property
    def state(self) -> MediaPlayerState | None:
        """Return whether the window is showing anything."""
        power = self.coordinator.data.power
        if power is None:
            return None
        return MediaPlayerState.PLAYING if power else MediaPlayerState.OFF

    @property
    def media_title(self) -> str | None:
        """Return the name of the current view."""
        return self.coordinator.data.view_title

    @property
    def media_artist(self) -> str | None:
        """Return where the current view was filmed."""
        return self.coordinator.data.view_location

    @property
    def media_image_url(self) -> str | None:
        """Return the current view's thumbnail, used as cover art."""
        return self.coordinator.data.view_image_url

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Offer volume only while the window is reporting a range for it.

        A card that shows a volume slider doing nothing cannot be told from a
        fault, and the number entity for the same setting already goes
        unavailable rather than pretending.
        """
        if self._volume is None:
            return self._BASE_FEATURES
        return self._BASE_FEATURES | self._VOLUME_FEATURES

    @property
    def volume_level(self) -> float | None:
        """Return the landscape volume as a fraction of its reported range."""
        level = self._volume
        return level.fraction if level is not None else None

    @property
    def _volume(self) -> Level | None:
        return Level.from_wire(self.coordinator.data.quick_settings.get(VOLUME_SETTING))

    async def async_turn_on(self) -> None:
        """Wake the display."""
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self) -> None:
        """Put the display to sleep."""
        await self.coordinator.async_set_power(False)

    async def async_media_next_track(self) -> None:
        """Move to the next view."""
        await self.coordinator.async_send_command("next_view")

    async def async_media_previous_track(self) -> None:
        """Move to the previous view."""
        await self.coordinator.async_send_command("previous_view")

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the landscape volume from a fraction of its reported range."""
        await self._async_write_volume(self._required_volume().at_fraction(volume))

    async def async_volume_up(self) -> None:
        """Raise the landscape volume by one step the window recognises."""
        await self._async_step_volume(1)

    async def async_volume_down(self) -> None:
        """Lower the landscape volume by one step the window recognises."""
        await self._async_step_volume(-1)

    async def _async_step_volume(self, steps: int) -> None:
        await self._async_write_volume(self._required_volume().stepped(steps))

    def _required_volume(self) -> Level:
        """Return the reported volume range, or say why there is none.

        Returning quietly would accept the command, write nothing, and log
        nothing - indistinguishable from a window that ignored it.
        """
        if (level := self._volume) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="setting_not_reported",
            )
        return level

    async def _async_write_volume(self, value: int) -> None:
        if (current := self._volume) is not None and current.value == value:
            # Already there. The step clamps at the ends, so repeating a press
            # at the ceiling would otherwise write the same value each time.
            return
        await self.coordinator.async_set_setting(VOLUME_SETTING, value)
