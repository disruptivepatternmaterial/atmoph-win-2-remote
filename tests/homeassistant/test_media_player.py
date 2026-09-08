"""Media player tests for Atmoph Window.

The media player reads and writes nothing the other platforms do not, so what
is worth testing is the translation: state, the three metadata fields, and
volume, which is the only one that changes units on the way through.
"""

from __future__ import annotations

from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.components.media_player import (
    DOMAIN as MEDIA_PLAYER_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_MEDIA_NEXT_TRACK,
    SERVICE_MEDIA_PREVIOUS_TRACK,
    SERVICE_TURN_OFF,
    SERVICE_VOLUME_DOWN,
    SERVICE_VOLUME_SET,
    SERVICE_VOLUME_UP,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmoph_window.media_player import VOLUME_SETTING

from .fakes import VIEW_IMAGE_URL, FakeBluetooth

ENTITY_ID = "media_player.living_room_window"


async def call(hass: HomeAssistant, service: str, **data: object) -> None:
    """Invoke a media player service against the window."""
    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN,
        service,
        {ATTR_ENTITY_ID: ENTITY_ID} | data,
        blocking=True,
    )


async def test_the_window_presents_the_current_view_as_something_playing(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Title, location and thumbnail are what the Media Control card renders."""
    state = hass.states.get(ENTITY_ID)

    assert state.state == MediaPlayerState.PLAYING
    assert state.attributes["media_title"] == "Kyoto"
    assert state.attributes["media_artist"] == "Kyoto, Japan"
    assert state.attributes["entity_picture"] or VIEW_IMAGE_URL


async def test_a_sleeping_display_is_off_rather_than_idle(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Nothing is on screen, so the player is off, not paused on something."""
    await call(hass, SERVICE_TURN_OFF)
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == MediaPlayerState.OFF


async def test_play_and_pause_are_not_offered(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The protocol has no such command, and claiming one would be a lie.

    A card that offers a pause button doing nothing is worse than a card
    without one, because the user cannot tell it from a fault.
    """
    features = hass.states.get(ENTITY_ID).attributes["supported_features"]

    assert not features & MediaPlayerEntityFeature.PLAY
    assert not features & MediaPlayerEntityFeature.PAUSE
    assert features & MediaPlayerEntityFeature.NEXT_TRACK
    assert features & MediaPlayerEntityFeature.VOLUME_SET


async def test_stepping_a_view_uses_the_same_tokens_the_buttons_do(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Transport controls are the view-stepping commands under another name."""
    fake_bluetooth.client.commands.clear()

    await call(hass, SERVICE_MEDIA_NEXT_TRACK)
    await call(hass, SERVICE_MEDIA_PREVIOUS_TRACK)
    await hass.async_block_till_done()

    # The tail, because reconnecting re-sends the `C` announce and the
    # point here is which tokens the transport controls map to.
    assert fake_bluetooth.client.commands[-2:] == [b"FW", b"BW"]


async def test_volume_reaches_both_ends_of_the_window_own_range(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The reported range is 0-24, so a percentage would miss at both ends.

    This is the acceptance criterion for the media player: a slider that
    cannot select the quietest or loudest setting cannot mute the window or
    turn it up.
    """
    level = loaded_entry.runtime_data.data.quick_settings[VOLUME_SETTING]
    assert (level["min"], level["max"]) == (0, 24)
    assert hass.states.get(ENTITY_ID).attributes[ATTR_MEDIA_VOLUME_LEVEL] == 0.5
    fake_bluetooth.client.writes.clear()

    await call(hass, SERVICE_VOLUME_SET, volume_level=0.0)
    await call(hass, SERVICE_VOLUME_SET, volume_level=1.0)
    await hass.async_block_till_done()

    assert fake_bluetooth.client.settings_writes == [
        b'{"LandscapeVolumeLevel":0}',
        b'{"LandscapeVolumeLevel":24}',
    ]


async def test_volume_steps_by_one_unit_the_window_recognises(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """A tenth of the range is not a step; the device's own unit is."""
    fake_bluetooth.client.writes.clear()

    await call(hass, SERVICE_VOLUME_UP)
    await hass.async_block_till_done()
    await call(hass, SERVICE_VOLUME_DOWN)
    await hass.async_block_till_done()

    assert fake_bluetooth.client.settings_writes == [
        b'{"LandscapeVolumeLevel":13}',
        b'{"LandscapeVolumeLevel":12}',
    ]


async def test_a_dropped_connection_makes_the_player_unavailable(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Cover art from a minute ago is not evidence of what is on screen now."""
    await fake_bluetooth.client.disconnect()
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
