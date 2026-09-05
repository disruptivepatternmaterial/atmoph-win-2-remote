"""Device identity and config entry migration tests for Atmoph Window.

Identity is the one thing this integration cannot get wrong quietly. A window
registered under one key on Monday and another on Tuesday is two devices as far
as Home Assistant is concerned, and the recorder keys history on the entity id
the registry hands out. So these tests assert against the entity and device
registries rather than against coordinator state.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmoph_window.config_flow import AtmophWindowConfigFlow
from custom_components.atmoph_window.const import (
    CONF_ADVERTISED_NAME,
    CONF_DEVICE_UUID,
    DOMAIN,
)

from .fakes import WINDOW_ADDRESS, WINDOW_NAME, FakeBluetooth, device_uuid_for

# One entity per platform, so a migration that only rewrites some of them is
# caught. The renamed entity id stands in for everything a user has invested in
# an entity: it is what the recorder keys history on.
LEGACY_ENTITIES = {
    "switch": ("display", "switch.the_window_i_renamed"),
    "sensor": ("current_view", "sensor.living_room_window_current_view"),
    "number": ("screen_brightness", "number.living_room_window_screen_brightness"),
}


def unique_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    """Return the unique id of every entity the entry owns."""
    registry = er.async_get(hass)
    return {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


def only_device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry:
    """Return the entry's device, asserting it registered exactly one."""
    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(devices) == 1, f"expected one device, got {len(devices)}"
    return devices[0]


def seed_legacy_registry_entries(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, str]:
    """Register the rows 0.2.1 would have left behind, and return their ids."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, WINDOW_NAME)},
        manufacturer="Atmoph",
        model="Window 2",
        name=WINDOW_NAME,
    )
    registry = er.async_get(hass)
    entity_ids = {"device": device.id}
    for platform, (key, entity_id) in LEGACY_ENTITIES.items():
        registered = registry.async_get_or_create(
            platform,
            DOMAIN,
            f"{WINDOW_NAME}_{key}",
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=entity_id.split(".", 1)[1],
        )
        entity_ids[key] = registered.entity_id
    return entity_ids


def assert_every_entity_is_keyed_on(
    hass: HomeAssistant, entry: MockConfigEntry, key: str
) -> set[str]:
    """Assert the entry's entities all key on one value, and return their ids."""
    registered = unique_ids(hass, entry)
    expected = {f"{key}_{entity}" for entity, _ in LEGACY_ENTITIES.values()}
    assert expected <= registered
    assert all(unique_id.startswith(f"{key}_") for unique_id in registered)
    return registered


async def test_a_0_2_1_entry_migrates_onto_the_device_uuid_and_keeps_its_entity_ids(
    hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    legacy_config_entry: MockConfigEntry,
) -> None:
    """The upgrade has to move identity without orphaning any history.

    Recorder history follows the entity id, and the device page follows the
    device registry row, so both have to be the same objects afterwards -
    rewritten in place rather than replaced.
    """
    legacy_config_entry.add_to_hass(hass)
    before = seed_legacy_registry_entries(hass, legacy_config_entry)

    assert await hass.config_entries.async_setup(legacy_config_entry.entry_id)
    await hass.async_block_till_done()

    assert legacy_config_entry.state is ConfigEntryState.LOADED
    assert legacy_config_entry.version == AtmophWindowConfigFlow.VERSION
    assert legacy_config_entry.data[CONF_DEVICE_UUID] == device_uuid_for()

    registry = er.async_get(hass)
    for platform, (key, _) in LEGACY_ENTITIES.items():
        entity_id = registry.async_get_entity_id(
            platform, DOMAIN, f"{device_uuid_for()}_{key}"
        )
        assert entity_id == before[key], f"{key} lost its entity id"
        assert (
            registry.async_get_entity_id(platform, DOMAIN, f"{WINDOW_NAME}_{key}")
            is None
        )

    device = only_device(hass, legacy_config_entry)
    assert device.id == before["device"]
    assert device.identifiers == {(DOMAIN, device_uuid_for())}


async def test_a_fresh_entry_reaches_the_same_identity_as_an_upgraded_one(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """An install and an upgrade have to be indistinguishable afterwards."""
    assert loaded_entry.version == AtmophWindowConfigFlow.VERSION
    assert loaded_entry.data[CONF_DEVICE_UUID] == device_uuid_for()

    assert_every_entity_is_keyed_on(hass, loaded_entry, device_uuid_for())
    assert only_device(hass, loaded_entry).identifiers == {(DOMAIN, device_uuid_for())}


async def test_a_window_that_never_reports_a_uuid_stays_on_its_advertised_name(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, config_entry: MockConfigEntry
) -> None:
    """An empty identity payload has to produce one answer, not a coin toss.

    Restarting must not move the window, because the fallback is a function of
    the stored entry rather than of what the last read happened to contain.
    """
    fake_bluetooth.device_uuid_reported = False
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.runtime_data.data.device_uuid is None
    assert config_entry.data[CONF_DEVICE_UUID] is None
    first = assert_every_entity_is_keyed_on(hass, config_entry, WINDOW_NAME)
    assert only_device(hass, config_entry).identifiers == {(DOMAIN, WINDOW_NAME)}

    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert unique_ids(hass, config_entry) == first
    assert only_device(hass, config_entry).identifiers == {(DOMAIN, WINDOW_NAME)}


async def test_a_uuid_reported_late_is_adopted_without_losing_the_entity_ids(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, config_entry: MockConfigEntry
) -> None:
    """Issue #12: the ordering that used to silently replace the device.

    A window that answers its first identity read with an empty payload and a
    later one with a UUID must end up on the UUID, but through the same
    migration an upgrade uses, so the entities and the device survive the move.
    """
    fake_bluetooth.device_uuid_reported = False
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    before = {
        item.unique_id.removeprefix(f"{WINDOW_NAME}_"): item.entity_id
        for item in er.async_entries_for_config_entry(registry, config_entry.entry_id)
    }
    device_id = only_device(hass, config_entry).id

    fake_bluetooth.device_uuid_reported = True
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.data[CONF_DEVICE_UUID] == device_uuid_for()
    after = {
        item.unique_id.removeprefix(f"{device_uuid_for()}_"): item.entity_id
        for item in er.async_entries_for_config_entry(registry, config_entry.entry_id)
    }
    assert after == before
    assert only_device(hass, config_entry).id == device_id


async def test_an_adopted_uuid_is_never_replaced_by_a_different_one(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, config_entry: MockConfigEntry
) -> None:
    """A window answering with another UUID is another window, not a rename.

    Following it would orphan the history of the window the entry was set up
    for, so the first UUID an entry adopts is the one it keeps.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_DEVICE_UUID: "device-uuid-adopted-earlier"},
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.data[CONF_DEVICE_UUID] == "device-uuid-adopted-earlier"
    assert only_device(hass, config_entry).identifiers == {
        (DOMAIN, "device-uuid-adopted-earlier")
    }


async def test_the_device_records_no_bluetooth_connection(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The only address a window has rotates, so none belongs in the registry.

    Connections are matched across integrations, so a stored resolvable private
    address would within the minute name whatever device the controller handed
    it to next.
    """
    assert only_device(hass, loaded_entry).connections == set()


async def test_a_unique_id_already_taken_blocks_the_whole_move(
    hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    legacy_config_entry: MockConfigEntry,
) -> None:
    """A blocked rekey has to move nothing rather than some of it.

    Home Assistant refuses a unique id another entity of the same platform
    already holds, and that check spans every config entry while the rewrite
    walks only one. Applying the moves one at a time would strand the entry
    across two identity namespaces, where every retry fails the same way.
    """
    legacy_config_entry.add_to_hass(hass)
    before = seed_legacy_registry_entries(hass, legacy_config_entry)

    # Uniqueness is scoped to the platform, not to the owning entry, so an
    # unowned row is enough to reproduce the collision.
    er.async_get(hass).async_get_or_create(
        "switch", DOMAIN, f"{device_uuid_for()}_display"
    )

    assert await hass.config_entries.async_setup(legacy_config_entry.entry_id)
    await hass.async_block_till_done()

    # Usable and wholly on its old key, not half on each.
    assert legacy_config_entry.state is ConfigEntryState.LOADED
    assert legacy_config_entry.data[CONF_DEVICE_UUID] is None
    registry = er.async_get(hass)
    for platform, (key, _) in LEGACY_ENTITIES.items():
        assert (
            registry.async_get_entity_id(platform, DOMAIN, f"{WINDOW_NAME}_{key}")
            == before[key]
        )
    assert only_device(hass, legacy_config_entry).identifiers == {(DOMAIN, WINDOW_NAME)}


async def test_a_uuid_another_entry_holds_is_refused_rather_than_shared(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Two entries on one UUID cannot both work, so the second must fail loudly.

    Sharing it would leave the younger entry loaded, entity-less because every
    unique id collides, and holding a connection to a window the older entry
    already owns - invisible on the device page and unreachable by a service
    call, because neither resolves to an entry that owns nothing.
    """
    assert loaded_entry.data[CONF_DEVICE_UUID] == device_uuid_for()

    # A second entry that reaches the same physical window, as a duplicate
    # advertised name or a re-add under a variant would.
    duplicate = MockConfigEntry(
        domain=DOMAIN,
        title="Bedroom Window",
        unique_id="Bedroom Window",
        version=AtmophWindowConfigFlow.VERSION,
        data={
            CONF_ADVERTISED_NAME: "Bedroom Window",
            "address": WINDOW_ADDRESS,
            CONF_DEVICE_UUID: None,
        },
    )
    duplicate.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(duplicate.entry_id)
    await hass.async_block_till_done()

    assert duplicate.state is ConfigEntryState.SETUP_ERROR
    assert duplicate.data[CONF_DEVICE_UUID] is None
    assert unique_ids(hass, duplicate) == set()
    # The window that was already working is untouched.
    assert_every_entity_is_keyed_on(hass, loaded_entry, device_uuid_for())


async def test_a_registry_left_behind_by_a_lost_write_is_repaired(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, config_entry: MockConfigEntry
) -> None:
    """Adoption spans two stores that flush at different times.

    Config entries are written after a second and the registries after ten, or
    a hundred and eighty during startup, so a crash in between leaves the entry
    claiming a UUID while the registries still hold name-keyed rows. Gating the
    rekey on the UUID being unset would then never repair it, and the next run
    would build a duplicate set of entities beside the orphaned ones.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_DEVICE_UUID: device_uuid_for()}
    )
    before = seed_legacy_registry_entries(hass, config_entry)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for platform, (key, _) in LEGACY_ENTITIES.items():
        assert (
            registry.async_get_entity_id(platform, DOMAIN, f"{device_uuid_for()}_{key}")
            == before[key]
        ), f"{key} was not repaired onto the stored key"
        assert (
            registry.async_get_entity_id(platform, DOMAIN, f"{WINDOW_NAME}_{key}")
            is None
        )
    assert only_device(hass, config_entry).id == before["device"]


async def test_an_emptied_device_row_is_removed_rather_than_left_on_screen(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, config_entry: MockConfigEntry
) -> None:
    """A stale row referencing a live entry is never pruned automatically.

    Home Assistant only orphans a device row that no entity and no config entry
    references, so a name-keyed row left beside the adopted one stays visible
    forever as a second Atmoph device with nothing on it.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_DEVICE_UUID: device_uuid_for()}
    )
    devices = dr.async_get(hass)
    # The shape two runs of a gated rekey leave: the adopted row holding the
    # entities, and an emptied name-keyed row beside it. Renaming onto an
    # identifier this entry already holds is refused, so the stale row can only
    # be removed, never merged.
    adopted = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, device_uuid_for())},
        name=WINDOW_NAME,
    )
    er.async_get(hass).async_get_or_create(
        "switch",
        DOMAIN,
        f"{device_uuid_for()}_display",
        config_entry=config_entry,
        device_id=adopted.id,
    )
    stale = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, WINDOW_NAME)},
        name=WINDOW_NAME,
    )
    assert stale.id != adopted.id

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert devices.async_get(stale.id) is None
    assert only_device(hass, config_entry).identifiers == {(DOMAIN, device_uuid_for())}


async def test_a_0_2_1_entry_whose_window_reports_no_uuid_still_reaches_version_2(
    hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    legacy_config_entry: MockConfigEntry,
) -> None:
    """The schema bump cannot wait on a value only a connection can supply."""
    fake_bluetooth.device_uuid_reported = False
    legacy_config_entry.add_to_hass(hass)
    before = seed_legacy_registry_entries(hass, legacy_config_entry)

    assert await hass.config_entries.async_setup(legacy_config_entry.entry_id)
    await hass.async_block_till_done()

    assert legacy_config_entry.version == AtmophWindowConfigFlow.VERSION
    assert legacy_config_entry.data[CONF_DEVICE_UUID] is None
    assert (
        er.async_get(hass).async_get_entity_id(
            "switch", DOMAIN, f"{WINDOW_NAME}_display"
        )
        == before["display"]
    )
    assert only_device(hass, legacy_config_entry).identifiers == {(DOMAIN, WINDOW_NAME)}
