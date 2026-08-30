"""Async Atmoph Window BLE client."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any, Protocol

from .protocol import (
    COMMAND_UUID,
    FOCUSING_VIEW_UUID,
    IDENTITY_UUID,
    PANORAMA_ROLE_UUID,
    POWER_UUID,
    QUICK_SETTINGS_UUID,
    VIEW_IMAGE_UUID,
    VIEW_LOCATION_UUID,
    VIEW_TITLE_UUID,
    AtmophState,
    JsonObjectStream,
    decode_text,
    encode_command,
    encode_setting,
)

# The display reports its new state within about a second of accepting a
# toggle. A second toggle sent too soon after the first is silently discarded,
# so the retry pause is deliberately longer than the confirmation window.
_POWER_POLL_INTERVAL = 0.5
_POWER_POLLS = 6
_POWER_RETRY_DELAY = 2.0
_POWER_ATTEMPTS = 2


class BleakClientLike(Protocol):
    """Subset of BleakClient used by the protocol client."""

    @property
    def is_connected(self) -> bool: ...

    async def read_gatt_char(self, char_specifier: str) -> bytearray: ...

    async def write_gatt_char(
        self, char_specifier: str, data: bytes, response: bool
    ) -> None: ...

    async def start_notify(
        self, char_specifier: str, callback: Callable[[Any, bytearray], None]
    ) -> None: ...

    async def stop_notify(self, char_specifier: str) -> None: ...


class AtmophClient:
    """Read state and send commands over an established BLE connection."""

    _NOTIFY_UUIDS = (
        PANORAMA_ROLE_UUID,
        VIEW_TITLE_UUID,
        VIEW_IMAGE_UUID,
        VIEW_LOCATION_UUID,
        FOCUSING_VIEW_UUID,
        QUICK_SETTINGS_UUID,
        POWER_UUID,
    )

    def __init__(
        self,
        client: BleakClientLike,
        on_update: Callable[[AtmophState], None] | None = None,
    ) -> None:
        self._client = client
        self._on_update = on_update
        self._quick_settings_stream = JsonObjectStream()
        self._lock = asyncio.Lock()
        self.state = AtmophState()

    @property
    def is_connected(self) -> bool:
        """Return whether the BLE transport is connected."""
        return self._client.is_connected

    async def initialize(self) -> AtmophState:
        """Subscribe to state changes and perform the app's initial reads."""
        for uuid in self._NOTIFY_UUIDS:
            await self._client.start_notify(uuid, self._notification)
        await self.refresh()
        await self.send_command("connect_notify")
        return self.state

    async def close(self) -> None:
        """Stop notifications before the owning coordinator disconnects."""
        for uuid in self._NOTIFY_UUIDS:
            with contextlib.suppress(Exception):
                await self._client.stop_notify(uuid)

    async def refresh(self) -> AtmophState:
        """Read all stable state exposed by the Android app."""
        async with self._lock:
            self.state.apply_identity(await self._read(IDENTITY_UUID))
            self.state.panorama_role = decode_text(await self._read(PANORAMA_ROLE_UUID))
            self.state.view_title = decode_text(await self._read(VIEW_TITLE_UUID))
            self.state.view_image_url = decode_text(await self._read(VIEW_IMAGE_UUID))
            self.state.view_location = decode_text(await self._read(VIEW_LOCATION_UUID))
            self.state.apply_power(await self._read(POWER_UUID))
            raw_settings = decode_text(await self._read(QUICK_SETTINGS_UUID))
            if raw_settings:
                settings = json.loads(raw_settings)
                if isinstance(settings, dict):
                    self.state.apply_quick_settings(settings)
        self._publish()
        return self.state

    async def send_command(self, name: str) -> None:
        """Send one verified remote-control command."""
        async with self._lock:
            await self._client.write_gatt_char(
                COMMAND_UUID, encode_command(name), response=True
            )

    async def set_power(self, desired: bool) -> None:
        """Set display power safely even though the protocol only has a toggle.

        The window drops a toggle sent within roughly a second of the previous
        one, so an unconfirmed write is retried after a longer pause rather
        than treated as a failure.
        """
        self.state.apply_power(await self._read(POWER_UUID))
        if self.state.power == desired:
            self._publish()
            return

        for attempt in range(_POWER_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_POWER_RETRY_DELAY)
            await self.send_command("sleep_toggle")
            if await self._await_power(desired):
                self._publish()
                return

        raise TimeoutError("Window did not confirm the requested display power state")

    async def _await_power(self, desired: bool) -> bool:
        """Poll the power characteristic until it reports the desired state."""
        for _ in range(_POWER_POLLS):
            await asyncio.sleep(_POWER_POLL_INTERVAL)
            self.state.apply_power(await self._read(POWER_UUID))
            if self.state.power == desired:
                return True
        return False

    async def set_setting(self, name: str, value: bool | int | str) -> None:
        """Write one quick-menu setting."""
        async with self._lock:
            await self._client.write_gatt_char(
                QUICK_SETTINGS_UUID, encode_setting(name, value), response=True
            )

    async def _read(self, uuid: str) -> bytes:
        return bytes(await self._client.read_gatt_char(uuid))

    def _notification(self, sender: Any, data: bytearray) -> None:
        uuid = str(getattr(sender, "uuid", sender)).lower()
        payload = bytes(data)
        try:
            if uuid == POWER_UUID:
                self.state.apply_power(payload)
            elif uuid == VIEW_TITLE_UUID:
                self.state.view_title = decode_text(payload)
            elif uuid == VIEW_IMAGE_UUID:
                self.state.view_image_url = decode_text(payload)
            elif uuid == VIEW_LOCATION_UUID:
                self.state.view_location = decode_text(payload)
            elif uuid == PANORAMA_ROLE_UUID:
                self.state.panorama_role = decode_text(payload)
            elif uuid == QUICK_SETTINGS_UUID:
                for settings in self._quick_settings_stream.feed(payload):
                    self.state.apply_quick_settings(settings)
            else:
                return
        except (UnicodeDecodeError, ValueError):
            return
        self._publish()

    def _publish(self) -> None:
        if self._on_update is not None:
            self._on_update(self.state)
