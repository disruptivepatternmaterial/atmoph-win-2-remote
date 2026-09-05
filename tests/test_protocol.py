"""Offline protocol tests."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

import pytest

from custom_components.atmoph_window import client as client_module
from custom_components.atmoph_window.client import AtmophClient
from custom_components.atmoph_window.protocol import (
    COMMAND_UUID,
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
    Level,
    encode_command,
    encode_setting,
)


def test_protocol_layer_is_home_assistant_free() -> None:
    """The wire protocol must stay usable outside Home Assistant.

    Importing the client and protocol modules must not drag in Home
    Assistant, so the recovered protocol can be reused and tested on its own.
    """
    assert "homeassistant" not in sys.modules


def test_commands_match_android_app() -> None:
    """Named controls encode to the app's ASCII tokens."""
    assert encode_command("sleep_toggle") == b"S"
    assert encode_command("next_view") == b"FW"
    assert encode_command("previous_view") == b"BW"
    assert encode_command("menu") == b"M"


def test_commands_cover_the_tokens_that_have_no_entity() -> None:
    """Double tap and search are reachable only through the command service."""
    assert encode_command("double_tap") == b"DT"
    assert encode_command("search") == b"VS"


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown Atmoph command"):
        encode_command("factory_reset")


def test_setting_is_compact_single_key_json() -> None:
    assert encode_setting("ScreenBrightness", 6) == b'{"ScreenBrightness":6}'
    assert encode_setting("WidgetsVisible", True) == b'{"WidgetsVisible":true}'


def test_unknown_setting_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown Atmoph setting"):
        encode_setting("FirmwareUpdate", "url")


def test_json_stream_reassembles_and_separates_documents() -> None:
    stream = JsonObjectStream()
    assert stream.feed(b'{"ScreenBright') == []
    assert stream.feed(b'ness":{"min":1,"max":10,') == []
    documents = stream.feed(b'"value":6}}{"SoundOnly":false}')
    assert documents == [
        {"ScreenBrightness": {"min": 1, "max": 10, "value": 6}},
        {"SoundOnly": False},
    ]


def test_json_stream_recovers_from_prefix_noise() -> None:
    stream = JsonObjectStream()
    assert stream.feed(b'noise{"SoundOnly":') == []
    assert stream.feed(b"true}") == [{"SoundOnly": True}]


def test_json_stream_reassembles_a_character_split_across_packets() -> None:
    """A packet boundary ignores character boundaries.

    Atmoph's titles and locations are Japanese, so a multi-byte character
    landing across two notifications is ordinary. Decoding each packet on its
    own raises on both halves, and the client discards a payload it cannot
    decode, so the update would be lost rather than merely delayed.
    """
    document = '{"ViewTitle":"京都"}'.encode()
    split = document.index("京".encode()) + 1

    stream = JsonObjectStream()
    assert stream.feed(document[:split]) == []
    assert stream.feed(document[split:]) == [{"ViewTitle": "京都"}]


def test_json_stream_recovers_after_malformed_bytes() -> None:
    """Undecodable input costs the buffer, not the stream."""
    stream = JsonObjectStream()
    with pytest.raises(UnicodeDecodeError):
        stream.feed(b'{"ViewTitle":"\xff\xfe"}')
    assert stream.feed(b'{"SoundOnly":true}') == [{"SoundOnly": True}]


def test_json_stream_enforces_size_limit() -> None:
    stream = JsonObjectStream(max_size=8)
    with pytest.raises(ValueError, match="size limit"):
        stream.feed(b'{"unfinished":')


def test_state_parses_identity_power_and_levels() -> None:
    state = AtmophState()
    state.apply_identity(b"device-uuid,Living Room")
    state.apply_power(b"true")
    level = Level.from_wire({"min": 1, "max": 10, "value": 6})
    assert state.device_uuid == "device-uuid"
    assert state.name == "Living Room"
    assert state.power is True
    assert level == Level(minimum=1, maximum=10, value=6)


def test_an_identity_without_a_uuid_reports_none_rather_than_a_placeholder() -> None:
    """The window that answers with an empty first field has no UUID to give.

    A blank standing in for the UUID would key the whole integration on the
    empty string, so the absence has to survive as an absence and let the
    caller decide what to do about it.
    """
    state = AtmophState()

    state.apply_identity(b",Living Room")

    assert state.device_uuid is None
    assert state.name == "Living Room"


def test_an_identity_carrying_only_a_uuid_leaves_the_name_unset() -> None:
    """The app slices the payload at index 36, so a short one has no name."""
    state = AtmophState()

    state.apply_identity(b"device-uuid")

    assert state.device_uuid == "device-uuid"
    assert state.name is None


def test_local_write_keeps_the_reported_bounds() -> None:
    """A level is written as a bare value but must stay a bounded object."""
    state = AtmophState()
    state.apply_quick_settings(
        {
            "ScreenBrightness": {"min": 1, "max": 10, "value": 6},
            "WidgetsVisible": True,
        }
    )

    state.apply_setting_write("ScreenBrightness", 9)
    state.apply_setting_write("WidgetsVisible", False)

    assert Level.from_wire(state.quick_settings["ScreenBrightness"]) == Level(
        minimum=1, maximum=10, value=9
    )
    assert state.quick_settings["WidgetsVisible"] is False


def test_invalid_power_payload_is_rejected() -> None:
    state = AtmophState()
    with pytest.raises(ValueError, match="Unexpected power payload"):
        state.apply_power(b"sleeping")


def test_view_id_splits_into_a_stable_id_and_a_render_revision() -> None:
    """The revision moves when Atmoph re-renders a view, so it is kept apart."""
    state = AtmophState()
    state.apply_view_id(b"LAT2_IUOV6NFQ/7206c70d")
    assert state.view_id == "LAT2_IUOV6NFQ"
    assert state.view_revision == "7206c70d"


def test_a_view_id_without_a_revision_is_still_an_id() -> None:
    """Only one report of this characteristic's format exists, so tolerate both."""
    state = AtmophState()
    state.apply_view_id(b"LAT2_IUOV6NFQ")
    assert state.view_id == "LAT2_IUOV6NFQ"
    assert state.view_revision is None


def test_an_empty_view_id_reports_nothing_rather_than_an_empty_string() -> None:
    state = AtmophState()
    state.apply_view_id(b"")
    assert state.view_id is None
    assert state.view_revision is None


class FakeBleakClient:
    """Minimal in-memory GATT peripheral."""

    is_connected = True

    def __init__(self, power: bool = True, view_id: bool = True) -> None:
        self.values: dict[str, bytes] = {
            IDENTITY_UUID: b"device-uuid,Living Room",
            PANORAMA_ROLE_UUID: b"N",
            VIEW_TITLE_UUID: b"Kyoto",
            VIEW_IMAGE_UUID: b"https://example.invalid/view.jpg",
            VIEW_LOCATION_UUID: b"Kyoto, Japan",
            POWER_UUID: b"true" if power else b"false",
            QUICK_SETTINGS_UUID: json.dumps(
                {
                    "ScreenBrightness": {"min": 1, "max": 10, "value": 6},
                    "WidgetsVisible": True,
                }
            ).encode(),
        }
        # A window that does not implement a characteristic has no entry for
        # it, so reading one raises, as a real read of an absent one does.
        if view_id:
            self.values[VIEW_ID_UUID] = b"LAT2_IUOV6NFQ/7206c70d"
        self.writes: list[tuple[str, bytes, bool]] = []
        self.notifications: dict[str, Callable[[Any, bytearray], None]] = {}
        self.reads: list[str] = []

    async def read_gatt_char(self, char_specifier: str) -> bytearray:
        self.reads.append(char_specifier)
        return bytearray(self.values[char_specifier])

    async def write_gatt_char(
        self, char_specifier: str, data: bytes, response: bool
    ) -> None:
        self.writes.append((char_specifier, data, response))
        if char_specifier == COMMAND_UUID and data == b"S":
            self.values[POWER_UUID] = (
                b"false" if self.values[POWER_UUID] == b"true" else b"true"
            )

    async def start_notify(
        self, char_specifier: str, callback: Callable[[Any, bytearray], None]
    ) -> None:
        self.notifications[char_specifier] = callback

    async def stop_notify(self, char_specifier: str) -> None:
        self.notifications.pop(char_specifier, None)


@pytest.mark.asyncio
async def test_initialize_reads_state_and_requests_notifications() -> None:
    peripheral = FakeBleakClient()
    updates: list[AtmophState] = []
    client = AtmophClient(peripheral, updates.append)
    state = await client.initialize()
    assert state.name == "Living Room"
    assert state.view_title == "Kyoto"
    assert state.power is True
    assert state.quick_settings["WidgetsVisible"] is True
    assert POWER_UUID in peripheral.notifications
    assert (COMMAND_UUID, b"C", True) in peripheral.writes
    assert updates


@pytest.mark.asyncio
async def test_initialize_reads_and_subscribes_to_the_view_id() -> None:
    peripheral = FakeBleakClient()
    client = AtmophClient(peripheral)
    state = await client.initialize()
    assert state.view_id_supported is True
    assert state.view_id == "LAT2_IUOV6NFQ"
    assert state.view_revision == "7206c70d"
    assert VIEW_ID_UUID in peripheral.notifications


@pytest.mark.asyncio
async def test_a_missing_view_id_characteristic_does_not_fail_initialization() -> None:
    """The app never binds this characteristic, so no window need implement it."""
    peripheral = FakeBleakClient(view_id=False)
    client = AtmophClient(peripheral)
    state = await client.initialize()

    assert state.view_id_supported is False
    assert state.view_id is None
    # Everything the app does bind still has to arrive.
    assert state.view_title == "Kyoto"
    assert state.power is True
    assert state.quick_settings["WidgetsVisible"] is True


@pytest.mark.asyncio
async def test_a_missing_view_id_characteristic_is_read_only_once() -> None:
    """Polling a window that lacks it must not raise on every update."""
    peripheral = FakeBleakClient(view_id=False)
    client = AtmophClient(peripheral)
    await client.initialize()
    peripheral.reads.clear()

    await client.refresh()

    assert VIEW_ID_UUID not in peripheral.reads


@pytest.mark.asyncio
async def test_a_refused_view_id_subscription_leaves_the_value_readable() -> None:
    """The one report of this characteristic gives it notify; the app's map does not."""

    class NoNotifyPeripheral(FakeBleakClient):
        """Rejects a subscription to the view id but answers a read of it."""

        async def start_notify(
            self, char_specifier: str, callback: Callable[[Any, bytearray], None]
        ) -> None:
            if char_specifier == VIEW_ID_UUID:
                raise RuntimeError("Characteristic does not support notifications")
            await super().start_notify(char_specifier, callback)

    peripheral = NoNotifyPeripheral()
    client = AtmophClient(peripheral)
    state = await client.initialize()

    assert VIEW_ID_UUID not in peripheral.notifications
    assert state.view_id_supported is True
    assert state.view_id == "LAT2_IUOV6NFQ"


@pytest.mark.asyncio
async def test_a_view_id_notification_updates_the_state() -> None:
    peripheral = FakeBleakClient()
    updates: list[AtmophState] = []
    client = AtmophClient(peripheral, updates.append)
    await client.initialize()

    peripheral.notifications[VIEW_ID_UUID](
        VIEW_ID_UUID, bytearray(b"LAT2_ABCDEF12/99ff0011")
    )

    assert client.state.view_id == "LAT2_ABCDEF12"
    assert client.state.view_revision == "99ff0011"


@pytest.mark.asyncio
async def test_power_control_is_idempotent_and_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)
    peripheral = FakeBleakClient(power=True)
    client = AtmophClient(peripheral)

    await client.set_power(True)
    assert peripheral.writes == []

    await client.set_power(False)
    assert peripheral.writes == [(COMMAND_UUID, b"S", True)]
    assert client.state.power is False


@pytest.mark.asyncio
async def test_power_control_retries_a_dropped_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A toggle the window silently discards is sent again, not reported failed."""

    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)

    class DroppingPeripheral(FakeBleakClient):
        """Ignores the first toggle, as the window does when toggled too fast."""

        def __init__(self) -> None:
            super().__init__(power=True)
            self._dropped = False

        async def write_gatt_char(
            self, char_specifier: str, data: bytes, response: bool
        ) -> None:
            if char_specifier == COMMAND_UUID and data == b"S" and not self._dropped:
                self._dropped = True
                self.writes.append((char_specifier, data, response))
                return
            await super().write_gatt_char(char_specifier, data, response)

    peripheral = DroppingPeripheral()
    client = AtmophClient(peripheral)

    await client.set_power(False)

    toggles = [w for w in peripheral.writes if w[1] == b"S"]
    assert len(toggles) == 2
    assert client.state.power is False


@pytest.mark.asyncio
async def test_power_control_gives_up_when_never_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresponsive window raises rather than reporting a state it never reached."""

    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)

    class UnresponsivePeripheral(FakeBleakClient):
        """Accepts every write at the ATT layer and changes nothing."""

        async def write_gatt_char(
            self, char_specifier: str, data: bytes, response: bool
        ) -> None:
            self.writes.append((char_specifier, data, response))

    client = AtmophClient(UnresponsivePeripheral())
    with pytest.raises(TimeoutError, match="did not confirm"):
        await client.set_power(False)
