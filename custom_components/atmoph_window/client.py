"""Async Atmoph Window BLE client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol

from .protocol import (
    COMMAND_UUID,
    FOCUSING_VIEW_UUID,
    IDENTITY_UUID,
    PANORAMA_ROLE_UUID,
    POWER_UUID,
    QUICK_SETTINGS_UUID,
    VIEW_ID_UUID,
    VIEW_IMAGE_UUID,
    VIEW_LOCATION_UUID,
    VIEW_TITLE_UUID,
    AtmophState,
    JsonObjectStream,
    TextStream,
    decode_text,
    encode_command,
    encode_setting,
)

# The display reports its new state within about a second of accepting a
# toggle. A second toggle sent too soon after the first is silently discarded,
# so the retry pause is deliberately longer than the confirmation window.
_LOGGER = logging.getLogger(__name__)

_POWER_POLL_INTERVAL = 0.5
_POWER_POLLS = 6
_POWER_RETRY_DELAY = 2.0
_POWER_ATTEMPTS = 2


class WrongWindowError(Exception):
    """Raised when a connection reached a window other than the configured one.

    Discovery matches on the advertised name and picks the strongest signal,
    which is the best an advertisement supports but is not identity. Only the
    device UUID read over GATT is, so it is checked before anything is written.
    """


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

    # The app declares this characteristic without ever binding it, and the
    # only report of a window answering it comes from different hardware, so
    # every interaction with it is best-effort.
    _OPTIONAL_NOTIFY_UUIDS = (VIEW_ID_UUID,)

    def __init__(
        self,
        client: BleakClientLike,
        on_update: Callable[[AtmophState], None] | None = None,
        state: AtmophState | None = None,
    ) -> None:
        self._client = client
        self._on_update = on_update
        self._quick_settings_stream = JsonObjectStream()
        # The two characteristics Atmoph fills with Japanese, and so the two a
        # packet boundary can split in the middle of a character.
        self._text_streams = {
            VIEW_TITLE_UUID: TextStream(),
            VIEW_LOCATION_UUID: TextStream(),
        }
        self._lock = asyncio.Lock()
        # A reconnect starts from what the last one knew, so a window that
        # answers one characteristic oddly does not cost every entity its
        # value on the way back up.
        self.state = state if state is not None else AtmophState()

    @property
    def is_connected(self) -> bool:
        """Return whether the BLE transport is connected."""
        return self._client.is_connected

    async def initialize(self, expect_device_uuid: str | None = None) -> AtmophState:
        """Subscribe to state changes and perform the app's initial reads.

        Subscribing precedes the reads, unlike the app, so a notification that
        fires while the reads are in flight is delivered rather than missed.

        `expect_device_uuid` is checked between the reads and the first write.
        Two windows can share an advertised name, and discovery resolves a name
        to whichever one is loudest, so writing before confirming identity can
        drive the wrong window.
        """
        for uuid in self._NOTIFY_UUIDS:
            await self._client.start_notify(uuid, self._notification)
        for uuid in self._OPTIONAL_NOTIFY_UUIDS:
            with contextlib.suppress(Exception):
                await self._client.start_notify(uuid, self._notification)

        await self.refresh()

        reported = self.state.device_uuid
        if expect_device_uuid is not None:
            if reported is not None and reported != expect_device_uuid:
                raise WrongWindowError(
                    f"Connected window reports {reported!r}, "
                    f"expected {expect_device_uuid!r}"
                )
            if reported is None:
                # Refusing here would be permanent: the stored identity never
                # changes, so a window that has stopped reporting its UUID
                # would fail every retry and the entry would never load again.
                # An absent UUID is also weak evidence of an impostor, since a
                # different window would answer with its own.
                _LOGGER.warning(
                    "%s did not report a device UUID, so this connection's "
                    "identity could not be confirmed",
                    expect_device_uuid,
                )

        await self.send_command("connect_notify")
        return self.state

    def set_update_callback(
        self, on_update: Callable[[AtmophState], None] | None
    ) -> None:
        """Attach the state callback once the peripheral's identity is known."""
        self._on_update = on_update

    def describe_gatt(self) -> list[dict[str, Any]]:
        """Describe the peripheral's GATT table.

        Structure only: service and characteristic UUIDs with their declared
        properties, never a value. What a window exposes is the open question
        behind the characteristics the app declares and never binds, and the
        answer is only obtainable from someone who owns one - so it belongs in
        the diagnostics they can hand over. It describes a model rather than a
        unit, which is why none of it needs redacting.
        """
        services = getattr(self._client, "services", None)
        if services is None:
            return []

        table: list[dict[str, Any]] = []
        for service in services:
            characteristics = [
                {
                    "uuid": str(char.uuid).lower(),
                    "properties": sorted(char.properties),
                }
                for char in service.characteristics
            ]
            characteristics.sort(key=lambda entry: entry["uuid"])
            table.append(
                {
                    "service": str(service.uuid).lower(),
                    "characteristics": characteristics,
                }
            )
        table.sort(key=lambda entry: entry["service"])
        return table

    async def close(self) -> None:
        """Stop notifications before the owning coordinator disconnects."""
        for uuid in self._NOTIFY_UUIDS + self._OPTIONAL_NOTIFY_UUIDS:
            with contextlib.suppress(Exception):
                await self._client.stop_notify(uuid)

    async def refresh(self) -> AtmophState:
        """Read all stable state exposed by the Android app.

        A failed *read* is a transport problem and propagates: the caller has
        to know the link is no longer answering. A value that reads fine and
        then will not parse is a different thing, and is tolerated per field.

        The distinction matters because one unexpected byte used to fail the
        whole refresh, and for an entry that had already adopted a device UUID
        that was permanent: the stored identity never changes, so every retry
        failed the same way and the window never loaded again. Losing one
        field is not a reason to have no entities.
        """
        async with self._lock:
            if (identity := await self._read_text(IDENTITY_UUID)) is not None:
                self.state.apply_identity(identity)
            if (role := await self._read_text(PANORAMA_ROLE_UUID)) is not None:
                self.state.panorama_role = role
            if (title := await self._read_text(VIEW_TITLE_UUID)) is not None:
                self.state.view_title = title
            if (image := await self._read_text(VIEW_IMAGE_UUID)) is not None:
                self.state.view_image_url = image
            if (location := await self._read_text(VIEW_LOCATION_UUID)) is not None:
                self.state.view_location = location
            self._apply_power_read(await self._read(POWER_UUID))
            self._apply_settings_read(await self._read(QUICK_SETTINGS_UUID))
            await self._read_view_id()
        return self.state

    async def _read_text(self, uuid: str) -> str | None:
        """Read one text field. None means it arrived garbled, not that it failed."""
        payload = await self._read(uuid)
        try:
            return decode_text(payload)
        except UnicodeDecodeError:
            _LOGGER.debug("Undecodable value read from %s", uuid)
            return None

    def _apply_power_read(self, payload: bytes) -> None:
        """Store display power, preferring unknown over a remembered value.

        The only way to set power is a toggle, so acting on a stale reading is
        how a display ends up inverted. Reporting that it is not known is the
        safer failure.
        """
        try:
            self.state.apply_power(payload)
        except (UnicodeDecodeError, ValueError):
            _LOGGER.debug("Unusable display power value; treating it as unknown")
            self.state.power = None

    def _apply_settings_read(self, payload: bytes) -> None:
        """Adopt a settings document, or keep what is known if none arrived.

        An empty or unparseable read carries no information, and treating it
        as an empty document would take every level and switch entity down
        with it while the refresh still reported success.
        """
        try:
            document = json.loads(decode_text(payload))
        except (UnicodeDecodeError, ValueError):
            _LOGGER.debug("Unparseable quick-settings read; keeping known values")
            return

        if isinstance(document, dict) and document:
            self.state.replace_quick_settings(document)

    async def _read_view_id(self) -> None:
        """Read the view id, treating an absent characteristic as normal.

        A window that does not implement it must still update, so the first
        failure stops the attempt for the life of the connection rather than
        raising an error the coordinator would report as an update failure.
        """
        if self.state.view_id_supported is False:
            return
        try:
            payload = await self._read(VIEW_ID_UUID)
        except Exception:
            self.state.view_id_supported = False
            return
        self.state.view_id_supported = True
        self.state.apply_view_id(payload)

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
        for attempt in range(_POWER_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_POWER_RETRY_DELAY)
            # Every attempt looks first, which is what the retry depends on.
            # A display that confirmed late and one that dropped the write are
            # indistinguishable until this read, and toggling without it turns
            # the first into an inversion: the display ends up where the user
            # did not ask for it and nothing raises.
            self._apply_power_read(await self._read(POWER_UUID))
            if self.state.power == desired:
                return
            await self.send_command("sleep_toggle")
            if await self._await_power(desired):
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
                if (text := self._text_streams[uuid].feed(payload)) is None:
                    return
                self.state.view_title = text
            elif uuid == VIEW_IMAGE_UUID:
                self.state.view_image_url = decode_text(payload)
            elif uuid == VIEW_LOCATION_UUID:
                if (text := self._text_streams[uuid].feed(payload)) is None:
                    return
                self.state.view_location = text
            elif uuid == VIEW_ID_UUID:
                self.state.view_id_supported = True
                self.state.apply_view_id(payload)
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
        """Hand unsolicited state to the owner.

        Only the notification path publishes. Reads and writes are all started
        by the coordinator, which publishes what they return, so publishing
        from them too would deliver every poll twice and reset the refresh
        timer behind the coordinator's back.
        """
        if self._on_update is not None:
            self._on_update(self.state)
