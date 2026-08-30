"""Config flow tests for Atmoph Window."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmoph_window.const import CONF_ADVERTISED_NAME, DOMAIN

from .fakes import (
    ROTATED_ADDRESS,
    WINDOW_ADDRESS,
    WINDOW_NAME,
    FakeBluetooth,
    make_service_info,
)


@pytest.fixture(autouse=True)
def _skip_setup():
    """Keep the flow tests to the flow: entry setup has its own tests."""
    with patch(
        "custom_components.atmoph_window.async_setup_entry", return_value=True
    ) as mock_setup:
        yield mock_setup


async def test_bluetooth_discovery_confirms_and_creates_entry(
    hass: HomeAssistant,
) -> None:
    """A discovered window is offered for confirmation, then configured."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=make_service_info()
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"name": WINDOW_NAME}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == WINDOW_NAME
    assert result["data"] == {
        CONF_ADVERTISED_NAME: WINDOW_NAME,
        "address": WINDOW_ADDRESS,
    }
    assert result["result"].unique_id == WINDOW_NAME


async def test_bluetooth_discovery_rejects_an_address_shaped_name(
    hass: HomeAssistant,
) -> None:
    """A BlueZ alias that only restates the address identifies nothing.

    The config entry keys on the advertised name because the address rotates,
    so a name that is really the address would key the entry on a value that
    does not survive the next rotation.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=make_service_info(name="AA-BB-CC-DD-EE-FF", address=WINDOW_ADDRESS),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_identify"


async def test_bluetooth_discovery_updates_the_address_of_a_known_window(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Rediscovery at a new address refreshes the entry instead of duplicating it."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=make_service_info(address=ROTATED_ADDRESS),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data["address"] == ROTATED_ADDRESS


async def test_user_flow_lists_visible_windows(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth
) -> None:
    """The manual flow offers the windows currently advertising the service."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADVERTISED_NAME: WINDOW_NAME}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_ADVERTISED_NAME: WINDOW_NAME,
        "address": WINDOW_ADDRESS,
    }


async def test_user_flow_ignores_devices_without_the_service(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth
) -> None:
    """Anything not advertising the Atmoph service is not a window."""
    fake_bluetooth.service_infos = [
        make_service_info(name="Someone's Headphones", service_uuids=[]),
    ]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_flow_aborts_when_nothing_is_visible(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth
) -> None:
    """A window that is not advertising cannot be configured by hand."""
    fake_bluetooth.service_infos = []

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
