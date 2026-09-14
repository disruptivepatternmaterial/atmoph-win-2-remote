"""Offline protocol tests."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import pytest

from custom_components.atmoph_window import client as client_module
from custom_components.atmoph_window.client import AtmophClient, WrongWindowError
from custom_components.atmoph_window.protocol import (
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
    Level,
    TextStream,
    encode_command,
    encode_setting,
)
from tests.window import (
    GATT_SERVICES,
    REPORTED_SETTINGS,
    TOGGLE_DROP_WINDOW,
    DisplayPower,
    FakeCharacteristic,
    FakeClock,
    Toggle,
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


def test_json_stream_keeps_working_after_the_size_limit() -> None:
    """The limit exists to bound memory, not to end the connection.

    A window that runs away once must not leave every later notification
    parsed against the runaway's leftovers.
    """
    stream = JsonObjectStream(max_size=24)
    with pytest.raises(ValueError, match="size limit"):
        stream.feed(b'{"a":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')

    assert stream.feed(b'{"SoundOnly":') == []
    assert stream.feed(b"true}") == [{"SoundOnly": True}]


def test_text_stream_holds_a_character_split_across_packets() -> None:
    """Both halves of a split character decode to nothing on their own."""
    stream = TextStream()
    title = "京都".encode()

    assert stream.feed(title[:1]) is None
    assert stream.feed(title[1:]) == "京都"


def test_text_stream_does_not_join_two_complete_values() -> None:
    """Only an incomplete character is held, so a value is never a prefix."""
    stream = TextStream()

    assert stream.feed(b"Kyoto") == "Kyoto"
    assert stream.feed(b"Tokyo") == "Tokyo"


def test_text_stream_recovers_after_malformed_bytes() -> None:
    """Undecodable input costs the half-received character, not the stream."""
    stream = TextStream()
    with pytest.raises(UnicodeDecodeError):
        stream.feed(b"\xff\xfe")

    assert stream.feed("京都".encode()) == "京都"


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


def test_a_level_that_is_not_a_level_is_not_guessed_at() -> None:
    """A malformed level has no value to show and no bounds to validate against."""
    assert Level.from_wire({"min": 1, "max": 10}) is None
    assert Level.from_wire({"min": "low", "max": "high", "value": "mid"}) is None
    assert Level.from_wire(True) is None


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


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace the client's waiting with a clock that records what it waited."""
    virtual = FakeClock()
    monkeypatch.setattr(client_module, "asyncio", virtual.patched_asyncio())
    return virtual


class FakeBleakClient:
    """Minimal in-memory GATT peripheral."""

    is_connected = True

    def __init__(
        self,
        power: bool = True,
        view_id: bool = True,
        clock: FakeClock | None = None,
        last_toggle_at: float | None = None,
        device_uuid: bool = True,
    ) -> None:
        self.services = GATT_SERVICES
        self.values: dict[str, bytes] = {
            # A window may answer identity with an empty first field,
            # reporting a name and no UUID at all.
            IDENTITY_UUID: (
                b"device-uuid,Living Room" if device_uuid else b",Living Room"
            ),
            PANORAMA_ROLE_UUID: b"N",
            VIEW_TITLE_UUID: b"Kyoto",
            VIEW_IMAGE_UUID: b"https://example.invalid/view.jpg",
            VIEW_LOCATION_UUID: b"Kyoto, Japan",
            POWER_UUID: b"true" if power else b"false",
            QUICK_SETTINGS_UUID: json.dumps(REPORTED_SETTINGS).encode(),
        }
        # A window that does not implement a characteristic has no entry for
        # it, so reading one raises, as a real read of an absent one does.
        if view_id:
            self.values[VIEW_ID_UUID] = b"LAT2_IUOV6NFQ/7206c70d"
        self.writes: list[tuple[str, bytes, bool]] = []
        self.notifications: dict[str, Callable[[Any, bytearray], None]] = {}
        self.reads: list[str] = []
        self.display = DisplayPower(
            clock if clock is not None else FakeClock(), last_toggle_at
        )

    @property
    def toggles(self) -> list[Toggle]:
        """Return every `S` write and whether the display acted on it."""
        return self.display.toggles

    async def read_gatt_char(self, char_specifier: str) -> bytearray:
        self.reads.append(char_specifier)
        return bytearray(self.values[char_specifier])

    async def write_gatt_char(
        self, char_specifier: str, data: bytes, response: bool
    ) -> None:
        # A write to the power characteristic is recorded and then discarded.
        # The window advertises write on it and ignores both directions, so
        # anything that relies on one has to fail here.
        self.writes.append((char_specifier, data, response))
        if char_specifier == COMMAND_UUID and data == b"S" and self.display.toggle():
            self.values[POWER_UUID] = (
                b"false" if self.values[POWER_UUID] == b"true" else b"true"
            )

    async def start_notify(
        self, char_specifier: str, callback: Callable[[Any, bytearray], None]
    ) -> None:
        self.notifications[char_specifier] = callback

    async def stop_notify(self, char_specifier: str) -> None:
        self.notifications.pop(char_specifier, None)

    def notify(self, uuid: str, payload: bytes, reported_as: str | None = None) -> None:
        """Deliver a notification the way bleak delivers one."""
        sender = FakeCharacteristic(uuid if reported_as is None else reported_as)
        self.notifications[uuid](sender, bytearray(payload))


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


def test_a_level_converts_to_and_from_a_fraction_at_both_ends() -> None:
    """Home Assistant wants a fraction; the window reports a bounded integer.

    The reported range is per-device and never 0-100, so treating the value as
    a percentage is wrong at both ends. Reaching the ends exactly is the part
    that matters: a slider that cannot select the loudest or the quietest
    setting is a slider that cannot turn the sound off.
    """
    level = Level(minimum=0, maximum=24, value=12)

    assert level.fraction == 0.5
    assert level.at_fraction(0.0) == 0
    assert level.at_fraction(1.0) == 24
    assert Level(minimum=0, maximum=24, value=24).fraction == 1.0
    assert Level(minimum=1, maximum=25, value=1).fraction == 0.0

    # A slider hands over whatever it likes; the ends still have to hold.
    assert level.at_fraction(-0.5) == 0
    assert level.at_fraction(1.5) == 24


def test_a_level_with_no_range_reports_zero_rather_than_dividing_by_it() -> None:
    """A window may report a setting it cannot vary."""
    assert Level(minimum=3, maximum=3, value=3).fraction == 0.0
    assert Level(minimum=3, maximum=3, value=3).at_fraction(1.0) == 3


def test_a_level_steps_by_device_units_and_stops_at_the_bounds() -> None:
    """A tenth of a six-value range is not a step the window recognises."""
    assert Level(minimum=0, maximum=5, value=2).stepped(1) == 3
    assert Level(minimum=0, maximum=5, value=2).stepped(-1) == 1
    assert Level(minimum=0, maximum=5, value=5).stepped(1) == 5
    assert Level(minimum=0, maximum=5, value=0).stepped(-1) == 0


@pytest.mark.asyncio
async def test_the_gatt_table_is_described_in_a_stable_order() -> None:
    """A report has to be diffable between two windows, so order is fixed.

    Discovery order is whatever the stack hands back, which would make two
    dumps of the same window differ. Properties are reported as declared,
    including on the characteristics known to advertise write and discard it,
    because a report that silently corrected them would hide the finding.
    """
    client = AtmophClient(FakeBleakClient())

    table = client.describe_gatt()

    assert [service["service"] for service in table] == [
        "401f7f45-2258-4f9b-8204-f8b301b4dcc5",
        "c1e0d952-12f7-4c84-b67d-fc26f55243a0",
    ]
    main = table[1]["characteristics"]
    assert [char["uuid"] for char in main] == sorted(char["uuid"] for char in main)
    power = next(char for char in main if char["uuid"] == POWER_UUID)
    assert power["properties"] == ["notify", "read", "write"]


@pytest.mark.asyncio
async def test_a_peripheral_without_a_service_table_describes_nothing() -> None:
    """A transport that exposes no services must not break a diagnostics dump."""
    peripheral = FakeBleakClient()
    del peripheral.services

    assert AtmophClient(peripheral).describe_gatt() == []


@pytest.mark.asyncio
async def test_a_full_read_drops_a_setting_the_window_no_longer_reports() -> None:
    """A read returns the whole document, so what is missing is gone.

    Merging a full read keeps serving a value nothing on the device stands
    behind - a slider for a setting the window dropped, reporting bounds it
    no longer has.
    """
    peripheral = FakeBleakClient()
    client = AtmophClient(peripheral)
    await client.initialize()
    assert "LedBrightness" in client.state.quick_settings

    shrunk = {k: v for k, v in REPORTED_SETTINGS.items() if k != "LedBrightness"}
    peripheral.values[QUICK_SETTINGS_UUID] = json.dumps(shrunk).encode()
    await client.refresh()

    assert "LedBrightness" not in client.state.quick_settings
    assert "ScreenBrightness" in client.state.quick_settings


@pytest.mark.asyncio
async def test_a_notification_adds_to_what_is_known_rather_than_replacing_it() -> None:
    """A push is unsolicited, so it is trusted to add but not to subtract.

    The window is documented to echo the whole document, but nothing forces
    it to, and dropping every key a partial push omitted would be a worse
    failure than briefly keeping one the next read will clear.
    """
    peripheral = FakeBleakClient()
    client = AtmophClient(peripheral)
    await client.initialize()

    peripheral.notify(QUICK_SETTINGS_UUID, json.dumps({"SoundOnly": True}).encode())

    assert client.state.quick_settings["SoundOnly"] is True
    assert "LedBrightness" in client.state.quick_settings


@pytest.mark.asyncio
async def test_a_window_reporting_no_uuid_fails_the_identity_check() -> None:
    """Saying nothing must not be a way to pass a check about who you are.

    Discovery resolves a shared advertised name to whichever window is
    loudest. If an absent UUID were treated as unknown-and-therefore-fine,
    the check would be defeated by silence and the entry would write to the
    wrong window.
    """
    peripheral = FakeBleakClient(device_uuid=False)
    client = AtmophClient(peripheral)

    with pytest.raises(WrongWindowError):
        await client.initialize("device-uuid")

    assert peripheral.writes == []


@pytest.mark.asyncio
async def test_only_notifications_are_pushed_to_the_owner() -> None:
    """Reads and writes return their result; the callback is for surprises.

    Everything except a notification is started by the coordinator, which
    publishes what the call returns. Publishing from the call as well delivers
    every poll twice and resets the refresh timer behind the coordinator's
    back, so the callback has to stay reserved for unsolicited state.
    """
    peripheral = FakeBleakClient()
    updates: list[AtmophState] = []
    client = AtmophClient(peripheral, updates.append)

    await client.initialize()
    await client.refresh()
    await client.set_power(False)
    await client.set_setting("WidgetsVisible", False)
    assert updates == []

    peripheral.notify(VIEW_TITLE_UUID, b"Osaka")
    assert [state.view_title for state in updates] == ["Osaka"]


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
async def test_every_characteristic_the_app_binds_is_subscribed() -> None:
    """Subscribing is what makes this integration a push one.

    A characteristic left out here reports nothing until the next poll, and
    the focused-view one is easy to drop because nothing consumes it yet.
    """
    peripheral = FakeBleakClient()
    await AtmophClient(peripheral).initialize()

    assert set(peripheral.notifications) == {
        PANORAMA_ROLE_UUID,
        VIEW_TITLE_UUID,
        VIEW_IMAGE_UUID,
        VIEW_LOCATION_UUID,
        FOCUSING_VIEW_UUID,
        QUICK_SETTINGS_UUID,
        POWER_UUID,
        VIEW_ID_UUID,
    }


# Every notifying characteristic, the payload a window sends on it, and the
# state it must land in. Named fields rather than "something changed", so a
# dispatcher that routes one characteristic into another's field fails.
NOTIFICATIONS = [
    (POWER_UUID, b"false", {"power": False}),
    (VIEW_TITLE_UUID, "富士山".encode(), {"view_title": "富士山"}),
    (
        VIEW_IMAGE_UUID,
        b"https://example.invalid/fuji.jpg",
        {"view_image_url": "https://example.invalid/fuji.jpg"},
    ),
    (VIEW_LOCATION_UUID, "静岡県".encode(), {"view_location": "静岡県"}),
    (PANORAMA_ROLE_UUID, b"L", {"panorama_role": "L"}),
    (
        VIEW_ID_UUID,
        b"LAT2_ABCDEF12/99ff0011",
        {"view_id": "LAT2_ABCDEF12", "view_revision": "99ff0011"},
    ),
    (
        QUICK_SETTINGS_UUID,
        b'{"SoundOnly":true}',
        {"quick_settings": {**REPORTED_SETTINGS, "SoundOnly": True}},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("uuid", "payload", "expected"), NOTIFICATIONS)
async def test_a_notification_reaches_the_state_field_that_owns_it(
    uuid: str, payload: bytes, expected: dict[str, object]
) -> None:
    """The window pushes; the integration declares `local_push`; prove the link.

    Comparing the whole state before and after means a characteristic the
    dispatcher stops routing changes nothing and fails here, and one routed
    into the wrong field changes the wrong entry and fails just as loudly.
    """
    peripheral = FakeBleakClient()
    updates: list[AtmophState] = []
    client = AtmophClient(peripheral, updates.append)
    await client.initialize()
    before = asdict(client.state)
    updates.clear()

    peripheral.notify(uuid, payload)

    after = asdict(client.state)
    assert {
        key: value for key, value in after.items() if before[key] != value
    } == expected
    assert updates == [client.state]


@pytest.mark.asyncio
async def test_a_notification_from_an_unread_characteristic_changes_nothing() -> None:
    """The app's focused-view JSON is subscribed and deliberately unused."""
    peripheral = FakeBleakClient()
    updates: list[AtmophState] = []
    client = AtmophClient(peripheral, updates.append)
    await client.initialize()
    before = asdict(client.state)
    updates.clear()

    peripheral.notify(FOCUSING_VIEW_UUID, b'{"focus":"views"}')

    assert asdict(client.state) == before
    assert updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uuid", "payload"),
    [
        (POWER_UUID, b"dozing"),
        (VIEW_TITLE_UUID, b"\xff\xfe"),
        (QUICK_SETTINGS_UUID, b"\xff\xfe"),
    ],
)
async def test_a_payload_that_cannot_be_parsed_costs_only_itself(
    uuid: str, payload: bytes
) -> None:
    """A notification arrives on bleak's thread, where raising helps nobody.

    The subscription would survive but the caller is a backend callback with
    nothing to catch it, so a window sending one payload the protocol does not
    describe has to cost that payload and not the connection.
    """
    peripheral = FakeBleakClient()
    updates: list[AtmophState] = []
    client = AtmophClient(peripheral, updates.append)
    await client.initialize()
    before = asdict(client.state)
    updates.clear()

    peripheral.notify(uuid, payload)

    assert asdict(client.state) == before
    assert updates == []
    # Still listening, and still able to take the next good payload.
    peripheral.notify(POWER_UUID, b"false")
    assert client.state.power is False


@pytest.mark.asyncio
async def test_a_notification_is_routed_by_the_characteristic_not_its_repr() -> None:
    """Bleak hands over the characteristic object, whose repr is not a UUID."""
    peripheral = FakeBleakClient()
    client = AtmophClient(peripheral)
    await client.initialize()

    sender = FakeCharacteristic(POWER_UUID)
    assert POWER_UUID not in str(sender)
    peripheral.notifications[POWER_UUID](sender, bytearray(b"false"))

    assert client.state.power is False


@pytest.mark.asyncio
async def test_a_notification_is_routed_whatever_case_the_uuid_arrives_in() -> None:
    """Bleak lower-cases the UUID it reports, and the client must not rely on it."""
    peripheral = FakeBleakClient()
    client = AtmophClient(peripheral)
    await client.initialize()

    peripheral.notify(POWER_UUID, b"false", reported_as=POWER_UUID.upper())

    assert client.state.power is False


@pytest.mark.asyncio
async def test_a_settings_document_split_across_packets_reaches_the_state() -> None:
    """Quick settings arrive in chunks, so nothing lands until the last one."""
    peripheral = FakeBleakClient()
    client = AtmophClient(peripheral)
    await client.initialize()
    document = b'{"ScreenBrightness":{"min":1,"max":25,"value":21}}'
    split = document.index(b"max")

    peripheral.notify(QUICK_SETTINGS_UUID, document[:split])
    assert client.state.quick_settings["ScreenBrightness"] == {
        "min": 1,
        "max": 25,
        "value": 6,
    }

    peripheral.notify(QUICK_SETTINGS_UUID, document[split:])
    assert client.state.quick_settings["ScreenBrightness"] == {
        "min": 1,
        "max": 25,
        "value": 21,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uuid", "attribute", "before", "value"),
    [
        (VIEW_TITLE_UUID, "view_title", "Kyoto", "京都"),
        (VIEW_LOCATION_UUID, "view_location", "Kyoto, Japan", "京都、日本"),
    ],
)
async def test_a_japanese_value_split_mid_character_still_reaches_the_state(
    uuid: str, attribute: str, before: str, value: str
) -> None:
    """Atmoph's titles and locations are Japanese and a boundary ignores characters.

    Decoding each packet on its own raises on both halves, so the value the
    window pushed would be dropped rather than merely delayed, and the sensor
    would sit on the previous view until the next poll.
    """
    peripheral = FakeBleakClient()
    client = AtmophClient(peripheral)
    await client.initialize()
    encoded = value.encode()

    peripheral.notify(uuid, encoded[:1])
    assert getattr(client.state, attribute) == before

    peripheral.notify(uuid, encoded[1:])
    assert getattr(client.state, attribute) == value


@pytest.mark.asyncio
async def test_two_whole_titles_in_a_row_do_not_run_together() -> None:
    """Holding a partial character must not turn into holding whole values."""
    peripheral = FakeBleakClient()
    client = AtmophClient(peripheral)
    await client.initialize()

    peripheral.notify(VIEW_TITLE_UUID, "京都".encode())
    peripheral.notify(VIEW_TITLE_UUID, "東京".encode())

    assert client.state.view_title == "東京"


@pytest.mark.asyncio
async def test_power_control_is_idempotent_and_confirmed(clock: FakeClock) -> None:
    peripheral = FakeBleakClient(power=True, clock=clock)
    client = AtmophClient(peripheral)

    await client.set_power(True)
    assert peripheral.writes == []

    await client.set_power(False)
    assert peripheral.writes == [(COMMAND_UUID, b"S", True)]
    assert client.state.power is False


@pytest.mark.asyncio
async def test_power_control_outwaits_a_display_that_drops_a_toggle(
    clock: FakeClock,
) -> None:
    """The pause before trying again has to outlast the window that swallowed it.

    A toggle within about a second of one that took effect is discarded with
    no ATT error, so retrying immediately - or after only the confirmation
    polling - would be discarded for the same reason as the first attempt.
    """
    # Someone reached for the window a moment ago, from the app or the panel,
    # so this toggle arrives while the display is still ignoring them.
    peripheral = FakeBleakClient(power=True, clock=clock, last_toggle_at=0.0)
    client = AtmophClient(peripheral)

    await client.set_power(False)

    dropped, retried = peripheral.toggles
    assert dropped.accepted is False
    assert retried.accepted is True
    assert retried.pause > TOGGLE_DROP_WINDOW
    assert client.state.power is False


@pytest.mark.asyncio
async def test_the_power_characteristic_is_never_written(clock: FakeClock) -> None:
    """It advertises write and discards both directions, so only the toggle works.

    A shortcut that wrote `true` here would look correct against a peripheral
    that stored whatever it was handed, and would do nothing to a window.
    """
    peripheral = FakeBleakClient(power=True, clock=clock)
    client = AtmophClient(peripheral)
    await client.initialize()

    await client.set_power(False)
    await client.set_power(True)

    assert client.state.power is True
    assert POWER_UUID not in {uuid for uuid, _, _ in peripheral.writes}


@pytest.mark.asyncio
async def test_power_control_gives_up_when_never_confirmed(clock: FakeClock) -> None:
    """An unresponsive window raises rather than reporting a state it never reached."""

    class UnresponsivePeripheral(FakeBleakClient):
        """Accepts every write at the ATT layer and changes nothing."""

        async def write_gatt_char(
            self, char_specifier: str, data: bytes, response: bool
        ) -> None:
            self.writes.append((char_specifier, data, response))

    client = AtmophClient(UnresponsivePeripheral(clock=clock))
    with pytest.raises(TimeoutError, match="did not confirm"):
        await client.set_power(False)
