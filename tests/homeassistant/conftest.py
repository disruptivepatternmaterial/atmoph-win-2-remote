"""Fixtures for the Home Assistant layer tests.

The stand-in packages `tests/conftest.py` installs for the protocol suite have
to go before anything here imports the integration for real. See this
directory's `__init__.py` for why the two suites cannot share a process.
"""

from __future__ import annotations

import sys

for _stub in ("custom_components.atmoph_window", "custom_components"):
    sys.modules.pop(_stub, None)

from collections.abc import Generator  # noqa: E402
from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.atmoph_window import client as client_module  # noqa: E402
from custom_components.atmoph_window.config_flow import (  # noqa: E402
    AtmophWindowConfigFlow,
)
from custom_components.atmoph_window.const import (  # noqa: E402
    CONF_ADVERTISED_NAME,
    CONF_DEVICE_UUID,
    DOMAIN,
)
from tests.window import FakeClock  # noqa: E402

from .fakes import WINDOW_ADDRESS, WINDOW_NAME, FakeBluetooth  # noqa: E402


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration from `custom_components/`."""


@pytest.fixture(autouse=True)
def _bluetooth_dependency(hass: HomeAssistant) -> None:
    """Report the Bluetooth integration as already set up.

    Setting it up for real wants a BlueZ D-Bus stack, and `fake_bluetooth`
    replaces every entry point this integration actually calls.
    """
    hass.config.components.add("bluetooth")


@pytest.fixture(autouse=True)
def clock() -> Generator[FakeClock]:
    """Replace the client's waiting with a clock that records what it waited.

    The display ignores a toggle sent within about a second of one that took
    effect, so the pause before a retry is behaviour rather than overhead.
    Collapsing every sleep to nothing would leave it untested.
    """
    virtual = FakeClock()
    with patch.object(client_module, "asyncio", virtual.patched_asyncio()):
        yield virtual


@pytest.fixture
def fake_bluetooth(clock: FakeClock) -> Generator[FakeBluetooth]:
    """Patch every Bluetooth entry point the integration reaches for."""
    fake = FakeBluetooth(clock)
    with (
        patch(
            "homeassistant.components.bluetooth.async_discovered_service_info",
            fake.discovered_service_info,
        ),
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            fake.ble_device_from_address,
        ),
        patch(
            "homeassistant.components.bluetooth.async_register_callback",
            fake.register_callback,
        ),
        patch(
            "custom_components.atmoph_window.coordinator.establish_connection",
            fake.establish_connection,
        ),
    ):
        yield fake


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a freshly created entry for a window that has never connected."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=WINDOW_NAME,
        unique_id=WINDOW_NAME,
        version=AtmophWindowConfigFlow.VERSION,
        data={
            CONF_ADVERTISED_NAME: WINDOW_NAME,
            "address": WINDOW_ADDRESS,
            CONF_DEVICE_UUID: None,
        },
    )


@pytest.fixture
def legacy_config_entry() -> MockConfigEntry:
    """Return an entry in the shape 0.2.1 wrote, keyed on the advertised name."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=WINDOW_NAME,
        unique_id=WINDOW_NAME,
        version=1,
        data={CONF_ADVERTISED_NAME: WINDOW_NAME, "address": WINDOW_ADDRESS},
    )


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up a window and return its loaded config entry."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
