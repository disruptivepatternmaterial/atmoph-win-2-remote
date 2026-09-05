"""Setup, coordinator, and entity tests for Atmoph Window."""

from __future__ import annotations

import json
import pathlib
from datetime import timedelta

import pytest
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.atmoph_window.config_flow import AtmophWindowConfigFlow
from custom_components.atmoph_window.const import (
    CONF_ADVERTISED_NAME,
    CONF_DEVICE_UUID,
    DOMAIN,
)
from custom_components.atmoph_window.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.atmoph_window.number import NUMBERS, AtmophNumber
from custom_components.atmoph_window.protocol import (
    COMMANDS,
    PANORAMA_ROLE_UUID,
    POWER_UUID,
    QUICK_SETTINGS_UUID,
    SETTING_KEYS,
    VIEW_ID_UUID,
    VIEW_IMAGE_UUID,
    VIEW_LOCATION_UUID,
    VIEW_TITLE_UUID,
)
from tests.window import TOGGLE_DROP_WINDOW, FakeClock

from .fakes import (
    NOTIFYING_CHARACTERISTICS,
    ROTATED_ADDRESS,
    SECOND_WINDOW_ADDRESS,
    VIEW_ID,
    VIEW_IMAGE_URL,
    VIEW_REVISION,
    WINDOW_ADDRESS,
    WINDOW_NAME,
    FakeBluetooth,
    device_uuid_for,
    make_service_info,
)

INTEGRATION = pathlib.Path(__file__).parents[2] / "custom_components" / DOMAIN


def entity_id_for(hass: HomeAssistant, platform: str, key: str) -> str:
    """Resolve an entity id from the unique id the integration assigns."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        platform, DOMAIN, f"{device_uuid_for()}_{key}"
    )
    assert entity_id is not None, f"no {platform} entity registered for {key}"
    return entity_id


async def test_setup_creates_entities_and_unload_releases_the_connection(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """A loaded window exposes its entities and gives the radio back on unload."""
    assert loaded_entry.state is ConfigEntryState.LOADED
    assert len(fake_bluetooth.clients) == 1
    assert len(fake_bluetooth.callbacks) == 1

    view = hass.states.get(entity_id_for(hass, "sensor", "current_view"))
    assert view.state == "Kyoto"
    assert view.attributes["location"] == "Kyoto, Japan"
    assert hass.states.get(entity_id_for(hass, "switch", "display")).state == STATE_ON
    brightness = hass.states.get(entity_id_for(hass, "number", "screen_brightness"))
    assert brightness.state == "6"

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    assert fake_bluetooth.client.is_connected is False
    assert fake_bluetooth.unregister_calls == 1
    assert fake_bluetooth.callbacks == []


async def test_setup_retries_when_the_window_is_not_visible(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, config_entry: MockConfigEntry
) -> None:
    """A window that is out of range defers setup instead of failing it.

    Home Assistant runs the entry's unload callbacks itself when setup raises,
    so anything registered before the first refresh would be unregistered
    twice, and the second unregister raises out of the `finally` that reports
    `ConfigEntryNotReady`. The entry would then never be retried.
    """
    fake_bluetooth.service_infos = []
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert fake_bluetooth.callbacks == []
    assert fake_bluetooth.unregister_calls == 0


async def test_unloading_stops_the_coordinator_refreshing(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """An unloaded window must not be reconnected by a refresh still in flight.

    An advertisement arriving while the entry unloads leaves a refresh queued
    behind it. Only `DataUpdateCoordinator.async_shutdown` marks the
    coordinator as shut down, so an override that forgets to call it lets that
    refresh reopen the connection the unload just closed.
    """
    coordinator = loaded_entry.runtime_data

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    connections = len(fake_bluetooth.clients)

    await coordinator.async_request_refresh()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=180))
    await hass.async_block_till_done()

    assert len(fake_bluetooth.clients) == connections
    assert fake_bluetooth.client.is_connected is False


async def test_coordinator_reconnects_at_a_rotated_address(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The window is followed by advertised name when its address rotates.

    Delivered through the fake's own dispatch rather than by calling the
    handler, so the scanning mode the integration registered with is part of
    what is being tested: a passive registration is handed no name at all and
    the window is never followed.
    """
    await fake_bluetooth.client.disconnect()
    fake_bluetooth.service_infos = [make_service_info(address=ROTATED_ADDRESS)]

    fake_bluetooth.advertise(make_service_info(address=ROTATED_ADDRESS))
    await hass.async_block_till_done()

    assert len(fake_bluetooth.clients) == 2
    assert fake_bluetooth.client.address == ROTATED_ADDRESS
    assert fake_bluetooth.client.is_connected


async def test_advertisements_from_other_devices_are_ignored(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Another window's advertisement must not steer this coordinator."""
    await fake_bluetooth.client.disconnect()

    fake_bluetooth.advertise(
        make_service_info(name="Bedroom Window", address=ROTATED_ADDRESS)
    )
    await hass.async_block_till_done()

    assert len(fake_bluetooth.clients) == 1


async def test_a_connection_the_window_drops_is_reopened_on_the_next_update(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The window goes away on its own, and says so through bleak's callback.

    The callback arrives on whichever thread bleak's backend runs on, so it
    has to reach the event loop before anything touches coordinator state. If
    the coordinator kept holding the dead client, every later update would be
    written to a transport that is gone.
    """
    coordinator = loaded_entry.runtime_data

    await fake_bluetooth.client.disconnect()
    await hass.async_block_till_done()
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert len(fake_bluetooth.clients) == 2
    assert fake_bluetooth.client.is_connected


async def test_a_nameless_advertisement_is_not_followed(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The same window is seen named and nameless seconds apart.

    The name rides in the scan response, so an advertisement without one says
    nothing about which window sent it. Following it would point the entry at
    an address that may belong to anything, and the address the entry already
    knows is the better guess precisely because a name once came with it.
    """
    coordinator = loaded_entry.runtime_data
    await fake_bluetooth.client.disconnect()
    # A cache with no name anywhere in it, so nothing matches by name and the
    # address the entry remembers is what the reconnection falls back on.
    fake_bluetooth.service_infos = [
        make_service_info(name="", address=WINDOW_ADDRESS),
        make_service_info(name="", address=ROTATED_ADDRESS),
    ]

    coordinator.async_handle_advertisement(
        make_service_info(name="", address=ROTATED_ADDRESS),
        BluetoothChange.ADVERTISEMENT,
    )
    await hass.async_block_till_done()
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert fake_bluetooth.client.address == WINDOW_ADDRESS


async def test_the_loudest_of_several_advertisements_is_the_one_connected_to(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Two windows can share a name, and only signal strength tells them apart.

    It is a weak identity and the integration knows it - the device UUID is
    checked before anything is written - but picking arbitrarily among them
    would reach a different window on every reconnection.
    """
    coordinator = loaded_entry.runtime_data
    await fake_bluetooth.client.disconnect()
    fake_bluetooth.service_infos = [
        make_service_info(address=WINDOW_ADDRESS, rssi=-90),
        make_service_info(address=ROTATED_ADDRESS, rssi=-40),
        make_service_info(address=SECOND_WINDOW_ADDRESS, rssi=-70),
    ]

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert fake_bluetooth.client.address == ROTATED_ADDRESS


async def test_a_window_never_seen_at_any_address_defers_setup(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth
) -> None:
    """With nothing advertising and no address on file there is nothing to try.

    The entry the config flow writes always carries the address it was
    discovered at, so this is the restored-from-disk case where that fallback
    is absent and the resolver has to report so rather than assume.
    """
    fake_bluetooth.service_infos = []
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=WINDOW_NAME,
        unique_id=WINDOW_NAME,
        version=AtmophWindowConfigFlow.VERSION,
        data={CONF_ADVERTISED_NAME: WINDOW_NAME, CONF_DEVICE_UUID: None},
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert fake_bluetooth.clients == []
    # Said in the resolver rather than left to whatever fails first when a
    # missing device is handed to the connector.
    assert "No connectable advertisement" in entry.reason


async def test_display_switch_reads_power_before_toggling(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The window only offers a toggle, so the current state decides the write."""
    entity_id = entity_id_for(hass, "switch", "display")
    client = fake_bluetooth.client
    client.writes.clear()
    assert hass.states.get(entity_id).state == STATE_ON

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    assert client.commands == []

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True
    )
    assert client.commands == [b"S"]
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_setting_switch_writes_the_quick_setting(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """A boolean quick setting is written as the app's single-key JSON."""
    entity_id = entity_id_for(hass, "switch", "widgets_visible")
    fake_bluetooth.client.writes.clear()
    assert hass.states.get(entity_id).state == STATE_ON

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True
    )

    assert fake_bluetooth.client.writes == [
        (QUICK_SETTINGS_UUID, b'{"WidgetsVisible":false}')
    ]
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_setting_switch_turns_a_setting_back_on(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Turning off and turning on are separate handlers and separate payloads."""
    entity_id = entity_id_for(hass, "switch", "sound_only")
    fake_bluetooth.client.writes.clear()
    assert hass.states.get(entity_id).state == STATE_OFF

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )

    assert fake_bluetooth.client.settings_writes == [b'{"SoundOnly":true}']
    assert hass.states.get(entity_id).state == STATE_ON


async def test_a_written_setting_is_confirmed_by_the_echo_it_comes_back_as(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The echo is the confirmation, and it is not always what was written.

    The optimistic update exists so a slider does not spring back while the
    window takes its second and a half to answer. It is a guess, and when the
    window disagrees - here because its ceiling moved since it last reported
    it - the entity has to end on the window's answer, not on the guess.
    """
    entity_id = entity_id_for(hass, "number", "screen_brightness")
    client = fake_bluetooth.client
    client.narrow_bounds("ScreenBrightness", maximum=10)
    assert hass.states.get(entity_id).attributes["max"] == 25

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entity_id, "value": 25},
        blocking=True,
    )
    assert hass.states.get(entity_id).state == "25"

    client.deliver_pending()
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == "10"
    assert state.attributes["max"] == 10


async def test_the_display_switch_outwaits_a_toggle_the_window_drops(
    hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
) -> None:
    """A dropped toggle is silent, so only the retry saves the switch.

    The display ignores a toggle sent within about a second of one that took
    effect, with no ATT error, so pressing the switch just after someone used
    the panel writes `S` to no effect and nothing says so.
    """
    entity_id = entity_id_for(hass, "switch", "display")
    client = fake_bluetooth.client
    client.display.touch()

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True
    )

    dropped, retried = client.toggles
    assert dropped.accepted is False
    assert retried.accepted is True
    assert retried.pause > TOGGLE_DROP_WINDOW
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_the_power_characteristic_is_never_written(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """It advertises write and discards both directions, so only the toggle works.

    A shortcut writing `true` here would look correct against a peripheral
    that stored whatever it was handed, and would do nothing to a window.
    """
    entity_id = entity_id_for(hass, "switch", "display")

    for service in ("turn_off", "turn_on"):
        await hass.services.async_call(
            "switch", service, {"entity_id": entity_id}, blocking=True
        )

    assert hass.states.get(entity_id).state == STATE_ON
    assert POWER_UUID not in {uuid for uuid, _ in fake_bluetooth.client.writes}


async def test_number_writes_a_value_inside_the_reported_range(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The window reports the bounds, and a value inside them is written."""
    entity_id = entity_id_for(hass, "number", "screen_brightness")
    fake_bluetooth.client.writes.clear()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entity_id, "value": 21},
        blocking=True,
    )

    assert fake_bluetooth.client.writes == [
        (QUICK_SETTINGS_UUID, b'{"ScreenBrightness":21}')
    ]
    assert hass.states.get(entity_id).state == "21"


# The bounds one reported device gave, which are per-device and nothing like
# the nought-to-ten a slider suggests.
DEVICE_BOUNDS = {
    "screen_brightness": (1, 25),
    "landscape_volume": (0, 24),
    "soundscape_volume": (0, 20),
    "led_brightness": (0, 20),
}


@pytest.mark.parametrize(("key", "bounds"), DEVICE_BOUNDS.items())
async def test_number_bounds_are_the_ones_the_window_reported(
    hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    loaded_entry: MockConfigEntry,
    key: str,
    bounds: tuple[int, int],
) -> None:
    """Bounds are per-device, so a range that is not the window's is a guess.

    A slider capped below what the display can do is silently unusable at the
    top, and one capped above it writes values the window clamps.
    """
    state = hass.states.get(entity_id_for(hass, "number", key))
    minimum, maximum = bounds

    assert (state.attributes["min"], state.attributes["max"]) == (minimum, maximum)


async def test_every_setting_the_window_reports_has_a_working_entity(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """A registered entity that is never available is worse than none at all.

    The window reports all nine settings the app knows, so every number and
    every setting switch has a value to show. One sitting on `unavailable`
    through a whole test run hides the platform being broken.
    """
    unavailable = [
        state.entity_id
        for state in hass.states.async_all(("number", "switch"))
        if state.state == "unavailable"
    ]

    assert unavailable == []


async def test_number_refuses_a_setting_the_window_has_never_reported(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, config_entry: MockConfigEntry
) -> None:
    """Nothing in the write format says whether an unreported key is a level."""
    fake_bluetooth.unreported_settings = frozenset({"LedBrightness"})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = entity_id_for(hass, "number", "led_brightness")
    assert hass.states.get(entity_id).state == "unavailable"
    description = next(item for item in NUMBERS if item.key == "led_brightness")
    entity = AtmophNumber(config_entry.runtime_data, description)

    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_native_value(4)

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "setting_not_reported"
    assert fake_bluetooth.client.settings_writes == []


async def test_number_rejects_a_value_outside_the_reported_range(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """An out-of-range value is a user error, not an unhandled exception.

    The `number` service guards its own `min`/`max`, so the entity's check only
    fires when a caller reaches past it or the window has narrowed the range
    since the last update. It still has to produce a message a person can read.
    """
    description = next(item for item in NUMBERS if item.key == "screen_brightness")
    entity = AtmophNumber(loaded_entry.runtime_data, description)

    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_native_value(99)

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "value_out_of_range"
    assert err.value.translation_placeholders == {
        "value": "99",
        "minimum": "1",
        "maximum": "25",
    }
    assert fake_bluetooth.client.writes[-1][0] != QUICK_SETTINGS_UUID


async def test_button_sends_the_mapped_command(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Each button writes the app's ASCII token to the command characteristic."""
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", "next_view")},
        blocking=True,
    )

    assert fake_bluetooth.client.commands[-1] == b"FW"


# Every notifying characteristic, the payload a window pushes on it, and the
# entity state or attribute a person then sees. Named individually, so a
# dispatcher that stops routing one of them fails on that one rather than
# being covered by whatever else happened to change.
PUSHED_STATE = [
    (POWER_UUID, b"false", "switch", "display", None, STATE_OFF),
    (VIEW_TITLE_UUID, "富士山".encode(), "sensor", "current_view", None, "富士山"),
    (
        VIEW_IMAGE_UUID,
        b"https://example.invalid/fuji.jpg",
        "sensor",
        "current_view",
        "image_url",
        "https://example.invalid/fuji.jpg",
    ),
    (
        VIEW_LOCATION_UUID,
        "静岡県".encode(),
        "sensor",
        "current_view",
        "location",
        "静岡県",
    ),
    (PANORAMA_ROLE_UUID, b"L", "sensor", "current_view", "panorama_role", "L"),
    (
        VIEW_ID_UUID,
        b"LAT2_ABCDEF12/99ff0011",
        "sensor",
        "view_id",
        None,
        "LAT2_ABCDEF12",
    ),
    (
        VIEW_ID_UUID,
        b"LAT2_ABCDEF12/99ff0011",
        "sensor",
        "view_id",
        "revision",
        "99ff0011",
    ),
    (
        QUICK_SETTINGS_UUID,
        b'{"ScreenBrightness":{"min":1,"max":25,"value":19}}',
        "number",
        "screen_brightness",
        None,
        "19",
    ),
    (
        QUICK_SETTINGS_UUID,
        b'{"DailyRoutineEnable":true}',
        "switch",
        "daily_routine",
        None,
        STATE_ON,
    ),
]


@pytest.mark.parametrize(
    ("uuid", "payload", "platform", "key", "attribute", "expected"), PUSHED_STATE
)
async def test_a_notification_reaches_the_entity_that_shows_it(
    hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    loaded_entry: MockConfigEntry,
    uuid: str,
    payload: bytes,
    platform: str,
    key: str,
    attribute: str | None,
    expected: str,
) -> None:
    """The manifest claims `local_push`, and this is the whole of that claim.

    Nothing here goes through a poll: the window notifies, bleak hands the
    payload to the client's dispatcher, and the value has to arrive in Home
    Assistant on its own.
    """
    entity_id = entity_id_for(hass, platform, key)

    fake_bluetooth.client.notify(uuid, payload)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert (state.state if attribute is None else state.attributes[attribute]) == (
        expected
    )


async def test_the_client_subscribes_to_every_notifying_characteristic(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """An unsubscribed characteristic reports nothing until the next poll."""
    assert set(fake_bluetooth.client.notifications) == set(NOTIFYING_CHARACTERISTICS)


async def test_a_settings_document_split_across_packets_reaches_the_entities(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Quick settings arrive in chunks, and half a document is not a document."""
    entity_id = entity_id_for(hass, "number", "landscape_volume")
    document = b'{"LandscapeVolumeLevel":{"min":0,"max":24,"value":23}}'
    split = document.index(b"max")

    fake_bluetooth.client.notify(QUICK_SETTINGS_UUID, document[:split])
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "12"

    fake_bluetooth.client.notify(QUICK_SETTINGS_UUID, document[split:])
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "23"


async def test_a_japanese_title_split_mid_character_reaches_the_sensor(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Atmoph's titles are Japanese and a packet boundary ignores characters.

    Decoding each packet on its own raises on both halves, so the view the
    window pushed would be lost rather than delayed and the sensor would show
    the previous one until the next poll a minute later.
    """
    entity_id = entity_id_for(hass, "sensor", "current_view")
    title = "京都".encode()

    fake_bluetooth.client.notify(VIEW_TITLE_UUID, title[:1])
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "Kyoto"

    fake_bluetooth.client.notify(VIEW_TITLE_UUID, title[1:])
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "京都"


async def test_view_id_sensor_reports_the_catalogue_id_and_its_revision(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """View titles repeat and translate, so automations need the catalogue id."""
    entity_id = entity_id_for(hass, "sensor", "view_id")
    state = hass.states.get(entity_id)

    assert state.state == VIEW_ID
    assert state.attributes["revision"] == VIEW_REVISION
    # The characteristic behind it is an unverified third-party observation, so
    # the entity is diagnostic rather than part of the primary control surface.
    entry = er.async_get(hass).async_get(entity_id)
    assert entry.entity_category is EntityCategory.DIAGNOSTIC


async def test_a_window_without_the_view_id_characteristic_still_sets_up(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, config_entry: MockConfigEntry
) -> None:
    """The app never binds this characteristic, so a window may not have it.

    An absent one has to cost nothing: setup succeeds, every other entity is
    unaffected, and no permanently unavailable sensor is left behind.
    """
    fake_bluetooth.view_id_supported = False
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.data.view_id_supported is False
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{device_uuid_for()}_view_id")
        is None
    )
    assert (
        hass.states.get(entity_id_for(hass, "sensor", "current_view")).state == "Kyoto"
    )
    assert hass.states.get(entity_id_for(hass, "switch", "display")).state == STATE_ON


async def test_a_view_id_that_stops_answering_does_not_fail_the_update(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """A window that drops the characteristic mid-life must still update."""
    coordinator = loaded_entry.runtime_data
    fake_bluetooth.client.values.pop(VIEW_ID_UUID)
    coordinator.data.view_id_supported = None

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.view_title == "Kyoto"
    assert coordinator.data.view_id_supported is False


async def test_diagnostics_redact_stable_identifiers(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Diagnostics are pasted into public issues, so nothing may identify a window.

    Named field by field and then checked again against the whole document,
    because a field dropped from the redaction list still exists - it just
    carries the real value, which reads as a plausible diagnostic until
    someone correlates it.
    """
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert diagnostics["last_update_success"] is True
    assert diagnostics["entry"] == {
        "advertised_name": REDACTED,
        "address": REDACTED,
        "device_uuid": REDACTED,
    }
    assert diagnostics["state"]["device_uuid"] == REDACTED
    assert diagnostics["state"]["name"] == REDACTED
    assert diagnostics["state"]["view_image_url"] == REDACTED

    serialised = json.dumps(diagnostics)
    for secret in (WINDOW_NAME, WINDOW_ADDRESS, VIEW_IMAGE_URL, device_uuid_for()):
        assert secret not in serialised

    # The view itself is not an identifier, and diagnostics with no state in
    # them are not worth collecting.
    assert diagnostics["state"]["view_title"] == "Kyoto"


async def test_every_registered_entity_has_a_translated_name(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A translation key with no entry renders the entity blank or raw."""
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    entries = er.async_entries_for_config_entry(
        er.async_get(hass), loaded_entry.entry_id
    )
    assert entries

    declared = {
        (platform, key) for platform, keys in strings["entity"].items() for key in keys
    }
    registered = set()
    for entry in entries:
        assert entry.translation_key, f"{entry.entity_id} has no translation key"
        assert strings["entity"][entry.domain][entry.translation_key]["name"]
        # Icons live in `icons.json` now, so nothing may carry an inline one.
        assert entry.original_icon is None
        registered.add((entry.domain, entry.translation_key))

    assert declared == registered


def test_english_translations_match_the_source_strings() -> None:
    """`translations/en.json` is the shipped copy of `strings.json`."""
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text())

    assert strings == english


def test_every_service_and_field_is_documented() -> None:
    """hassfest rejects a service or field with no name, and so does this.

    The local check exists because hassfest only runs in CI, where a missing
    string is found after the push rather than before it.
    """
    services = load_yaml_dict(str(INTEGRATION / "services.yaml"))
    strings = json.loads((INTEGRATION / "strings.json").read_text())

    assert set(services) == set(strings["services"])
    for name, schema in services.items():
        documented = strings["services"][name]
        assert documented["name"]
        assert documented["description"]
        assert set(schema["fields"]) == set(documented["fields"])
        for field in documented["fields"].values():
            assert field["name"]
            assert field["description"]


def test_service_pickers_offer_exactly_the_protocol_tokens() -> None:
    """A picker that drifts from the protocol offers a token the handler refuses."""
    services = load_yaml_dict(str(INTEGRATION / "services.yaml"))

    def options(service: str, field: str) -> set[str]:
        return set(services[service]["fields"][field]["selector"]["select"]["options"])

    assert options("send_command", "command") == set(COMMANDS)
    assert options("set_setting", "setting") == set(SETTING_KEYS)


def test_service_targets_carry_no_device_filter() -> None:
    """hassfest refuses a device filter on a service target."""
    services = load_yaml_dict(str(INTEGRATION / "services.yaml"))

    for schema in services.values():
        assert "device" not in schema["target"]
        assert schema["target"]["entity"] == {"integration": DOMAIN}


def test_declared_icons_belong_to_declared_entities() -> None:
    """An icon under an unknown translation key is silently never shown."""
    icons = json.loads((INTEGRATION / "icons.json").read_text())
    strings = json.loads((INTEGRATION / "strings.json").read_text())

    for platform, entries in icons["entity"].items():
        assert set(entries) <= set(strings["entity"][platform])
        for entry in entries.values():
            assert entry["default"].startswith("mdi:")
