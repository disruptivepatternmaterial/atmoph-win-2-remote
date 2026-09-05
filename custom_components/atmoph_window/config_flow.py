"""Config flow for Atmoph Window."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_ADVERTISED_NAME, CONF_DEVICE_UUID, DOMAIN
from .protocol import SERVICE_UUID


class AtmophWindowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure an Atmoph Window discovered over BLE.

    The entry's own unique id stays the advertised name even though the device
    UUID is the better identity, because this is the one place the UUID can
    never be had: rediscovery has only an advertisement to match against, and
    reading the UUID needs a connection. Entities and the device registry key
    on the UUID instead; see `identity.py`.
    """

    VERSION = 2

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle Home Assistant Bluetooth discovery."""
        name = discovery_info.name
        if not name or _looks_like_address(name, discovery_info.address):
            return self.async_abort(reason="cannot_identify")

        await self.async_set_unique_id(name)
        self._abort_if_unique_id_configured(
            updates={"address": discovery_info.address},
            reload_on_update=False,
        )
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered window."""
        if self._discovery is None:
            return self.async_abort(reason="cannot_identify")
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery.name,
                data={
                    CONF_ADVERTISED_NAME: self._discovery.name,
                    "address": self._discovery.address,
                    CONF_DEVICE_UUID: None,
                },
            )
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._discovery.name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a currently visible window."""
        infos = {
            info.name: info
            for info in bluetooth.async_discovered_service_info(
                self.hass, connectable=True
            )
            if info.name
            and not _looks_like_address(info.name, info.address)
            and SERVICE_UUID in {uuid.lower() for uuid in info.service_uuids}
        }
        if not infos:
            return self.async_abort(reason="no_devices_found")

        if user_input is not None:
            name = user_input[CONF_ADVERTISED_NAME]
            # The list is rebuilt on submit while the form was validated
            # against the one that was rendered, so a window that stopped
            # advertising in between passes validation and is gone by here.
            if (info := infos.get(name)) is None:
                return self.async_abort(reason="no_devices_found")
            await self.async_set_unique_id(name)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ADVERTISED_NAME: name,
                    "address": info.address,
                    CONF_DEVICE_UUID: None,
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADVERTISED_NAME): vol.In(sorted(infos))}
            ),
        )


def _strip_separators(value: str) -> str:
    """Reduce a string to its alphanumeric characters, lowercased."""
    return "".join(char for char in value.lower() if char.isalnum())


def _looks_like_address(name: str, address: str) -> bool:
    """Identify BlueZ aliases that are only a reformatted address."""
    return _strip_separators(name) == _strip_separators(address)
