"""Diagnostics tests for Atmoph Window.

Diagnostics get pasted into public issues, so what they must not contain
matters as much as what they must. They also carry the one thing about a
window that nobody without one can obtain, which is why the GATT table is
there unredacted.
"""

from __future__ import annotations

import json

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmoph_window.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.atmoph_window.protocol import POWER_UUID

from .fakes import (
    VIEW_IMAGE_URL,
    WINDOW_ADDRESS,
    WINDOW_NAME,
    FakeBluetooth,
    device_uuid_for,
)


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


async def test_diagnostics_carry_the_live_gatt_table(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """The table is the one thing only a window owner can supply.

    Which characteristics a Window 2 really exposes is still open, because the
    app declares several it never binds and the second service has only ever
    been seen on other hardware. A report that carries the table answers that
    from anyone who owns one, and it survives redaction because service and
    characteristic UUIDs describe a model rather than a unit.
    """
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert [service["service"] for service in diagnostics["gatt"]] == [
        "401f7f45-2258-4f9b-8204-f8b301b4dcc5",
        "c1e0d952-12f7-4c84-b67d-fc26f55243a0",
    ]
    power = next(
        char
        for service in diagnostics["gatt"]
        for char in service["characteristics"]
        if char["uuid"] == POWER_UUID
    )
    assert power["properties"] == ["notify", "read", "write"]


async def test_diagnostics_omit_the_gatt_table_while_disconnected(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, loaded_entry: MockConfigEntry
) -> None:
    """Reporting a remembered table as live would be worse than reporting none."""
    await fake_bluetooth.client.disconnect()
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert diagnostics["gatt"] == []
    assert diagnostics["last_update_success"] is False
