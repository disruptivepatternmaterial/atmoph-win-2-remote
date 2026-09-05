"""Setup, coordinator, and entity tests for Atmoph Window."""

from __future__ import annotations

import json
import pathlib
from datetime import timedelta

import pytest
from homeassistant.components.bluetooth import BluetoothChange
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

from custom_components.atmoph_window.const import DOMAIN
from custom_components.atmoph_window.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.atmoph_window.number import NUMBERS, AtmophNumber
from custom_components.atmoph_window.protocol import (
    COMMANDS,
    QUICK_SETTINGS_UUID,
    SETTING_KEYS,
    VIEW_ID_UUID,
)

from .fakes import (
    ROTATED_ADDRESS,
    VIEW_ID,
    VIEW_REVISION,
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
    """The window is followed by advertised name when its address rotates."""
    coordinator = loaded_entry.runtime_data
    await fake_bluetooth.client.disconnect()
    fake_bluetooth.service_infos = [make_service_info(address=ROTATED_ADDRESS)]

    coordinator.async_handle_advertisement(
        make_service_info(address=ROTATED_ADDRESS), BluetoothChange.ADVERTISEMENT
    )
    await hass.async_block_till_done()

    assert len(fake_bluetooth.clients) == 2
    assert fake_bluetooth.client.address == ROTATED_ADDRESS
    assert fake_bluetooth.client.is_connected


async def test_advertisements_from_other_devices_are_ignored(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Another window's advertisement must not steer this coordinator."""
    coordinator = loaded_entry.runtime_data
    await fake_bluetooth.client.disconnect()

    coordinator.async_handle_advertisement(
        make_service_info(name="Bedroom Window", address=ROTATED_ADDRESS),
        BluetoothChange.ADVERTISEMENT,
    )
    await hass.async_block_till_done()

    assert len(fake_bluetooth.clients) == 1


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


async def test_number_writes_a_value_inside_the_reported_range(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The window reports the bounds, and a value inside them is written."""
    entity_id = entity_id_for(hass, "number", "screen_brightness")
    fake_bluetooth.client.writes.clear()
    state = hass.states.get(entity_id)
    assert state.attributes["min"] == 1
    assert state.attributes["max"] == 10

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entity_id, "value": 9},
        blocking=True,
    )

    assert fake_bluetooth.client.writes == [
        (QUICK_SETTINGS_UUID, b'{"ScreenBrightness":9}')
    ]
    assert hass.states.get(entity_id).state == "9"


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
        "maximum": "10",
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
    """Diagnostics must not carry anything that identifies the owner's window."""
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert diagnostics["last_update_success"] is True
    assert WINDOW_NAME not in json.dumps(diagnostics)
    assert diagnostics["state"]["view_title"] == "Kyoto"
    assert diagnostics["state"]["device_uuid"] == "**REDACTED**"


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
