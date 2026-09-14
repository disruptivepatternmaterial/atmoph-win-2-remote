"""Shared lookups for the Home Assistant layer tests.

Entities are keyed on the device UUID the window reports, not on anything a
test chooses, so resolving an entity id means asking the registry. Kept here
rather than in one test module so splitting those modules does not mean
importing one test file from another.
"""

from __future__ import annotations

import pathlib

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.atmoph_window.const import DOMAIN

from .fakes import device_uuid_for

INTEGRATION = pathlib.Path(__file__).parents[2] / "custom_components" / DOMAIN


def entity_id_for(hass: HomeAssistant, platform: str, key: str) -> str:
    """Resolve an entity id from the unique id the integration assigns."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        platform, DOMAIN, f"{device_uuid_for()}_{key}"
    )
    assert entity_id is not None, f"no {platform} entity registered for {key}"
    return entity_id
