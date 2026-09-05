"""Offline protocol tests."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import pytest

from custom_components.atmoph_window import client as client_module
from custom_components.atmoph_window.client import AtmophClient
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


# The bounds one reported device gave. They are per-device and wider than a
# ten-step slider suggests, so a fake that reports a narrow range lets a
# hardcoded one pass.
REPORTED_SETTINGS = {
    "ScreenBrightness": {"min": 1, "max": 25, "value": 6},
    "LandscapeVolumeLevel": {"min": 0, "max": 24, "value": 12},
    "SoundscapeVolumeLevel": {"min": 0, "max": 20, "value": 8},
    "LedBrightness": {"min": 0, "max": 20, "value": 4},
    "CurrentDecoration": {"min": 0, "max": 19, "value": 3},
    "SoundscapeLayer": {"min": 0, "max": 5, "value": 2},
    "WidgetsVisible": True,
    "DailyRoutineEnable": False,
    "SoundOnly": False,
}

# A toggle sent within roughly a second of one that took effect is discarded,
# with no ATT error and no state change.
TOGGLE_DROP_WINDOW = 1.0


class FakeCharacteristic:
    """The object bleak hands to a notification callback.

    Bleak passes the characteristic the notification came from, never its
    UUID, so a fake that passes a bare string would let the client read
    `sender` directly and still pass every test.
    """

    def __init__(self, uuid: str) -> None:
        self.uuid = uuid

    def __str__(self) -> str:
        # Bleak's own string form is a description, not the UUID, so a client
        # that stringifies the characteristic has to fail here too.
        return f"<FakeCharacteristic at {id(self):#x}>"


class FakeClock:
    """A virtual clock that advances only when the client waits.

    The delays are part of what is under test - the pause before a retry has
    to outlast the window in which the display ignores a toggle - so they are
    recorded rather than collapsed to nothing.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, delay: float) -> None:
        """Advance the clock instead of waiting."""
        self.sleeps.append(delay)
        self.now += delay

    @property
    def last_sleep(self) -> float:
        """Return how long the client last waited before acting."""
        return self.sleeps[-1] if self.sleeps else 0.0


@dataclass(frozen=True, slots=True)
class Toggle:
    """One `S` write, and whether the display was still ignoring toggles."""

    at: float
    pause: float
    accepted: bool


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace the client's waiting with a clock that records what it waited."""
    virtual = FakeClock()
    monkeypatch.setattr(client_module.asyncio, "sleep", virtual.sleep)
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
    ) -> None:
        self.values: dict[str, bytes] = {
            IDENTITY_UUID: b"device-uuid,Living Room",
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
        self.toggles: list[Toggle] = []
        self.clock = clock if clock is not None else FakeClock()
        # None stands for a display nobody has touched recently, so the next
        # toggle lands outside the window in which one is discarded.
        self._accepted_at = last_toggle_at

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
        if char_specifier == COMMAND_UUID and data == b"S":
            self._toggle_display()

    def _toggle_display(self) -> None:
        """Apply a toggle unless it arrived while the display was ignoring them."""
        accepted = (
            self._accepted_at is None
            or self.clock.now - self._accepted_at > TOGGLE_DROP_WINDOW
        )
        self.toggles.append(Toggle(self.clock.now, self.clock.last_sleep, accepted))
        if not accepted:
            return
        self._accepted_at = self.clock.now
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
