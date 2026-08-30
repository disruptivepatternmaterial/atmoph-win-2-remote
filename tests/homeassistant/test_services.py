"""Service tests for Atmoph Window."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmoph_window.const import (
    CONF_ADVERTISED_NAME,
    DOMAIN,
    SERVICE_SEND_COMMAND,
    SERVICE_SET_SETTING,
)
from custom_components.atmoph_window.protocol import QUICK_SETTINGS_UUID

from .fakes import (
    SECOND_WINDOW_ADDRESS,
    SECOND_WINDOW_NAME,
    WINDOW_ADDRESS,
    WINDOW_NAME,
    FakeBluetooth,
    make_service_info,
)


def target(hass: HomeAssistant, window: str = WINDOW_NAME) -> dict[str, str]:
    """Return a service target naming one window by one of its entities."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{window}_display"
    )
    assert entity_id is not None
    return {"entity_id": entity_id}


@pytest.fixture
async def two_windows(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Configure a second window so a call has to say which one it means."""
    fake_bluetooth.service_infos = [
        make_service_info(),
        make_service_info(name=SECOND_WINDOW_NAME, address=SECOND_WINDOW_ADDRESS),
    ]
    second = MockConfigEntry(
        domain=DOMAIN,
        title=SECOND_WINDOW_NAME,
        unique_id=SECOND_WINDOW_NAME,
        data={
            CONF_ADVERTISED_NAME: SECOND_WINDOW_NAME,
            "address": SECOND_WINDOW_ADDRESS,
        },
    )
    config_entry.add_to_hass(hass)
    second.add_to_hass(hass)

    # Setting the component up brings up every entry of the domain, so both
    # windows load from the one call.
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert second.state is ConfigEntryState.LOADED
    return second


async def test_services_are_registered_when_a_window_loads(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_COMMAND)
    assert hass.services.has_service(DOMAIN, SERVICE_SET_SETTING)


async def test_send_command_writes_the_token_to_the_window(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """A command with a button is still reachable as a service."""
    fake_bluetooth.client.writes.clear()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        {"command": "menu"} | target(hass),
        blocking=True,
    )

    assert fake_bluetooth.client.commands == [b"M"]


@pytest.mark.parametrize(
    ("command", "token"),
    [("double_tap", b"DT"), ("search", b"VS")],
)
async def test_send_command_reaches_the_tokens_with_no_entity(
    hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    loaded_entry: MockConfigEntry,
    command: str,
    token: bytes,
) -> None:
    """These two tokens are exposed here instead of as redundant buttons."""
    fake_bluetooth.client.writes.clear()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        {"command": command} | target(hass),
        blocking=True,
    )

    assert fake_bluetooth.client.commands == [token]


async def test_send_command_rejects_an_unverified_token(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Only tokens recovered from the app may reach the window."""
    fake_bluetooth.client.writes.clear()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_COMMAND,
            {"command": "factory_reset"} | target(hass),
            blocking=True,
        )

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "unknown_command"
    assert err.value.translation_placeholders["command"] == "factory_reset"
    assert "double_tap" in err.value.translation_placeholders["commands"]
    assert fake_bluetooth.client.writes == []


async def test_send_command_requires_a_target(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """An untargeted call would otherwise silently reach nothing."""
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, SERVICE_SEND_COMMAND, {"command": "menu"}, blocking=True
        )

    assert err.value.translation_key == "no_window_targeted"


async def test_send_command_only_reaches_the_targeted_window(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, two_windows: MockConfigEntry
) -> None:
    """With several windows configured, a call must not broadcast."""
    for client in fake_bluetooth.clients:
        client.writes.clear()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        {"command": "next_view"} | target(hass, SECOND_WINDOW_NAME),
        blocking=True,
    )

    assert fake_bluetooth.client_at(SECOND_WINDOW_ADDRESS).commands == [b"FW"]
    assert fake_bluetooth.client_at(WINDOW_ADDRESS).commands == []


async def test_set_setting_writes_a_level_the_window_has_no_entity_for(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """`CurrentDecoration` is a catalogue index, so it is a service not a slider."""
    fake_bluetooth.client.writes.clear()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_SETTING,
        {"setting": "CurrentDecoration", "value": 11} | target(hass),
        blocking=True,
    )

    assert fake_bluetooth.client.settings_writes == [b'{"CurrentDecoration":11}']


async def test_set_setting_writes_a_boolean_as_a_boolean(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Nothing in the write format says which settings are booleans.

    The window's own report of the setting is what decides, so a value that
    arrives as a string still has to be written as JSON `false`.
    """
    fake_bluetooth.client.writes.clear()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_SETTING,
        {"setting": "WidgetsVisible", "value": "off"} | target(hass),
        blocking=True,
    )

    assert fake_bluetooth.client.settings_writes == [b'{"WidgetsVisible":false}']


async def test_set_setting_rejects_a_value_outside_the_reported_bounds(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Bounds are per-device, so only the window's own report may be trusted."""
    fake_bluetooth.client.writes.clear()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SETTING,
            {"setting": "SoundscapeLayer", "value": 9} | target(hass),
            blocking=True,
        )

    assert err.value.translation_key == "value_out_of_range"
    assert err.value.translation_placeholders == {
        "value": "9",
        "minimum": "0",
        "maximum": "5",
    }
    assert fake_bluetooth.client.settings_writes == []


async def test_set_setting_refuses_a_setting_the_window_has_not_reported(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Without a report there is no way to tell a level from a boolean."""
    fake_bluetooth.client.writes.clear()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SETTING,
            {"setting": "LedBrightness", "value": 4} | target(hass),
            blocking=True,
        )

    assert err.value.translation_key == "setting_not_reported"
    assert fake_bluetooth.client.settings_writes == []


async def test_set_setting_rejects_an_unknown_key(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Only keys recovered from the app's settings object may be written."""
    fake_bluetooth.client.writes.clear()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SETTING,
            {"setting": "FirmwareUpdate", "value": 1} | target(hass),
            blocking=True,
        )

    assert err.value.translation_key == "unknown_setting"
    assert err.value.translation_placeholders["setting"] == "FirmwareUpdate"
    assert fake_bluetooth.client.writes == []


async def test_a_service_call_publishes_the_new_state(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """A setting written by service must show up without waiting for a poll."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_SETTING,
        {"setting": "WidgetsVisible", "value": False} | target(hass),
        blocking=True,
    )

    assert loaded_entry.runtime_data.data.quick_settings["WidgetsVisible"] is False
    assert QUICK_SETTINGS_UUID in {uuid for uuid, _ in fake_bluetooth.client.writes}
