"""Stable identity for a window whose address and name are both unreliable.

Three facts about this device decide the scheme:

* The BLE address rotates, so it can never be identity.
* The advertised name rides in the scan response, so the same window is seen
  named and nameless seconds apart. It is the only value an advertisement
  reliably carries when it carries anything at all, which makes it the
  discovery key and nothing more.
* The device UUID read from the identity characteristic does not rotate, but
  it needs a connection, so it is unknown until after setup has begun.

So the entry is discovered by name and then adopts the UUID the first time a
window reports one. The switch is a registry migration rather than a new set
of identifiers: entity ids and their history survive it, which is what makes
adopting late safe rather than destructive.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONF_ADVERTISED_NAME, CONF_DEVICE_UUID, DOMAIN

_LOGGER = logging.getLogger(__name__)


@callback
def async_device_key(entry: ConfigEntry) -> str:
    """Return the value every entity and the device registry are keyed on.

    A window that has never reported a UUID stays on its advertised name for
    as long as that remains true, which is deterministic: the answer is a
    function of the stored entry alone, not of what the last refresh happened
    to read.
    """
    return entry.data.get(CONF_DEVICE_UUID) or entry.data[CONF_ADVERTISED_NAME]


async def async_adopt_device_uuid(
    hass: HomeAssistant, entry: ConfigEntry, device_uuid: str | None
) -> None:
    """Key the entry on the device UUID, carrying its registry rows across.

    Must run before the platforms are forwarded, so entities are created with
    the identity they will keep. A UUID is adopted once and never replaced: a
    window answering with a different one is a different window, and following
    it would orphan the history of the one the entry was set up for.
    """
    if not device_uuid or entry.data.get(CONF_DEVICE_UUID):
        return

    previous = entry.data[CONF_ADVERTISED_NAME]
    if previous != device_uuid:
        await _async_rekey_entities(hass, entry, previous, device_uuid)
        _async_rekey_device(hass, previous, device_uuid)
        _LOGGER.debug("Moved %s onto its device UUID", previous)

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_DEVICE_UUID: device_uuid}
    )


async def _async_rekey_entities(
    hass: HomeAssistant, entry: ConfigEntry, previous: str, device_uuid: str
) -> None:
    """Rewrite the unique id prefix of every entity the entry owns."""
    prefix = f"{previous}_"

    @callback
    def _rekey(registry_entry: er.RegistryEntry) -> dict[str, str] | None:
        if not registry_entry.unique_id.startswith(prefix):
            return None
        suffix = registry_entry.unique_id.removeprefix(prefix)
        return {"new_unique_id": f"{device_uuid}_{suffix}"}

    await er.async_migrate_entries(hass, entry.entry_id, _rekey)


@callback
def _async_rekey_device(hass: HomeAssistant, previous: str, device_uuid: str) -> None:
    """Rename the device registry row so the device page keeps its identity."""
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, previous)})
    if device is None:
        return
    # Renaming onto an identifier another row already holds is a collision the
    # registry refuses, and failing setup over it would be worse than leaving
    # the stale row for Home Assistant to prune once it owns no entities.
    if registry.async_get_device(identifiers={(DOMAIN, device_uuid)}) is not None:
        return
    registry.async_update_device(device.id, new_identifiers={(DOMAIN, device_uuid)})
