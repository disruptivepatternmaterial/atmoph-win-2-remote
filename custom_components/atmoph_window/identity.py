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

Adoption writes to two stores that flush independently — config entries after
a second, the registries after ten or, during startup, a hundred and eighty.
A crash between the two would otherwise leave the entry claiming a UUID while
the registries still hold name-keyed rows, with nothing to repair it. So the
registry side is reconciled against the stored key on every setup rather than
only on the run that adopts.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
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
    name = entry.data[CONF_ADVERTISED_NAME]

    if device_uuid and not entry.data.get(CONF_DEVICE_UUID):
        _async_refuse_claimed_uuid(hass, entry, device_uuid)
        # Leaving the entry on its name is recoverable; a half-moved registry
        # is not, so a blocked rekey must not be followed by the entry write.
        if not await _async_reconcile(hass, entry, name, device_uuid):
            return
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICE_UUID: device_uuid}
        )
        return

    # Repair a rekey whose registry write was lost after the entry write
    # landed. A no-op whenever the two stores already agree.
    await _async_reconcile(hass, entry, name, async_device_key(entry))


@callback
def _async_refuse_claimed_uuid(
    hass: HomeAssistant, entry: ConfigEntry, device_uuid: str
) -> None:
    """Stop a second entry adopting a UUID another one already holds.

    Both entries would otherwise key their entities identically, and Home
    Assistant rejects the duplicates rather than merging them — leaving the
    younger entry loaded, entity-less, and holding a connection to a window
    the older entry already owns. Failing loudly is the kinder outcome.
    """
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id != entry.entry_id and (
            other.data.get(CONF_DEVICE_UUID) == device_uuid
        ):
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="duplicate_device_uuid",
                translation_placeholders={"name": other.title},
            )


async def _async_reconcile(
    hass: HomeAssistant, entry: ConfigEntry, previous: str, key: str
) -> bool:
    """Move this entry's registry rows onto `key`, or leave them all alone."""
    if previous == key:
        return True
    if not _async_rekey_entities(hass, entry, previous, key):
        return False
    _async_rekey_device(hass, entry, previous, key)
    _LOGGER.debug("Moved %s onto %s", previous, key)
    return True


@callback
def _async_rekey_entities(
    hass: HomeAssistant, entry: ConfigEntry, previous: str, key: str
) -> bool:
    """Rewrite the unique id prefix of every entity, or of none of them.

    Home Assistant refuses a unique id already used by another entity of the
    same platform, and that check spans every config entry while this rewrite
    only walks one. So the targets are checked up front: applying the moves
    one at a time and hitting a conflict partway would strand the entry across
    two identity namespaces, where every retry fails identically.
    """
    registry = er.async_get(hass)
    prefix = f"{previous}_"
    moves: list[tuple[str, str]] = []

    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not item.unique_id.startswith(prefix):
            continue
        new_unique_id = f"{key}_{item.unique_id.removeprefix(prefix)}"
        taken = registry.async_get_entity_id(item.domain, DOMAIN, new_unique_id)
        if taken is not None and taken != item.entity_id:
            _LOGGER.warning(
                "Not moving %s onto %s: %s is already used by %s",
                previous,
                key,
                new_unique_id,
                taken,
            )
            return False
        moves.append((item.entity_id, new_unique_id))

    for entity_id, new_unique_id in moves:
        registry.async_update_entity(entity_id, new_unique_id=new_unique_id)
    return True


@callback
def _async_rekey_device(
    hass: HomeAssistant, entry: ConfigEntry, previous: str, key: str
) -> None:
    """Rename the device registry row so the device page keeps its identity.

    Scoped to this entry's rows. Identifiers are no longer unique across
    config entries, so a global lookup would refuse a rename that would have
    succeeded, and leave a second empty Atmoph device on screen for no reason.
    """
    registry = dr.async_get(hass)
    rows = dr.async_entries_for_config_entry(registry, entry.entry_id)

    device = next((row for row in rows if (DOMAIN, previous) in row.identifiers), None)
    if device is None:
        return

    existing = next((row for row in rows if (DOMAIN, key) in row.identifiers), None)
    if existing is not None:
        # Renaming onto an identifier this entry already holds is a collision
        # the registry refuses. The stale row is not pruned automatically
        # either, because it still references a live config entry, so an empty
        # one is removed here rather than left on the device page forever.
        if not er.async_entries_for_device(er.async_get(hass), device.id, True):
            registry.async_remove_device(device.id)
        return

    registry.async_update_device(device.id, new_identifiers={(DOMAIN, key)})
