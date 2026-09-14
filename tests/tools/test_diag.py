"""Offline verification of the BLE diagnostic in tools/.

What is checked here is everything the tool can get wrong without a device in
front of it: how it renders a value, what it concludes about the LEDs, how it
reads a write echo, and what a normalized report still contains. The scan, the
connect, and the real GATT enumeration cannot be reached without hardware and
are not covered.

Every name comes from the module that owns it, so a failure names the layer at
fault: the recovered tables in atmoph_catalog.py, the dump and its report in
atmoph_dump.py, the transport and the command line in atmoph_diag.py.

See tests/tools/fakes.py for the window these drive.
"""

from __future__ import annotations

import argparse
import json

import pytest

import atmoph_diag as diag
from atmoph_catalog import (
    KNOWN_CHARACTERISTICS,
    MASK,
    MAX_VALUE_BYTES,
    SECOND_SERVICE_UUID,
    SERVICE_LABELS,
)
from atmoph_dump import (
    CharacteristicDump,
    SettingDump,
    Target,
    WindowDump,
    analyse_leds,
    describe_settings,
    format_dump,
    normalize,
    render_value,
    report_header,
    slug,
)
from custom_components.atmoph_window import protocol
from tests.tools.fakes import WORKING_SETTINGS, FakeWindow


def test_a_utf8_value_renders_as_text() -> None:
    """A value that decodes cleanly is shown as text, at its real length."""
    rendered = render_value(b"Kamikochi")
    assert rendered.text == "Kamikochi"
    assert rendered.length == 9


def test_a_binary_value_renders_as_hex_alone() -> None:
    """A value that does not decode gets no text, only hex."""
    rendered = render_value(b"\x00\xff\x10\x80")
    assert rendered.text is None
    assert rendered.hex == "00 ff 10 80"


def test_nul_padding_is_stripped_from_text_and_kept_in_hex() -> None:
    """Padding has to disappear from the text and stay visible in the hex."""
    rendered = render_value(b"ok\x00\x00")
    assert rendered.text == "ok"
    assert rendered.hex == "6f 6b 00 00"


def test_an_oversized_value_is_flagged_and_keeps_its_real_length() -> None:
    """The hex is truncated, but the report still says how long the value was."""
    rendered = render_value(b"a" * (MAX_VALUE_BYTES + 10))
    assert rendered.truncated
    assert rendered.length == 522


def test_an_oversized_value_cut_mid_codepoint_is_still_read_as_text() -> None:
    """A display limit must not be reported as an undecodable value.

    `MAX_VALUE_BYTES` is not a codepoint boundary, so shortening the bytes
    before decoding lands inside a multi-byte character whenever the value is
    non-ASCII - and the report then presents perfectly good Japanese as binary
    for a reason that lives in this tool rather than on the device.
    """
    # 3 bytes per character, so the 512-byte mark falls inside one.
    raw = ("鴨" * 200).encode()
    assert MAX_VALUE_BYTES % len("鴨".encode()) != 0

    rendered = render_value(raw)

    assert rendered.truncated
    assert rendered.text is not None
    assert rendered.text.startswith("鴨鴨")


def test_a_control_character_falls_back_to_hex() -> None:
    """Text that decodes but is unprintable is not text worth printing."""
    assert render_value(b"a\x07b").text is None


# A document with one level, one boolean, one key of the wrong type, and one
# key no protocol constant knows about.
MIXED_DOCUMENT: dict[str, object] = {
    "ScreenBrightness": {"min": 1, "max": 25, "value": 18},
    "SoundOnly": False,
    "CurrentDecoration": "unexpected",
    "UnknownFromFirmware": 3,
}


@pytest.fixture
def described() -> list[SettingDump]:
    return describe_settings(MIXED_DOCUMENT)


@pytest.fixture
def by_key(described: list[SettingDump]) -> dict[str, SettingDump]:
    return {dump.key: dump for dump in described}


def test_every_known_setting_key_is_described(
    described: list[SettingDump],
) -> None:
    """A key the window omitted still gets a row, so its absence is visible."""
    assert len(described) == len(protocol.SETTING_KEYS) + 1


def test_described_settings_are_sorted(described: list[SettingDump]) -> None:
    """Report order must not depend on the order the window sent."""
    assert [dump.key for dump in described] == sorted(dump.key for dump in described)


def test_a_level_reports_its_kind_and_bounds(
    by_key: dict[str, SettingDump],
) -> None:
    assert by_key["ScreenBrightness"].kind == "level"
    assert by_key["ScreenBrightness"].maximum == 25


def test_a_boolean_reports_its_kind(by_key: dict[str, SettingDump]) -> None:
    assert by_key["SoundOnly"].kind == "bool"


def test_a_missing_key_reads_absent(by_key: dict[str, SettingDump]) -> None:
    assert by_key["LedBrightness"].kind == "absent"


def test_a_non_level_string_is_not_a_level(
    by_key: dict[str, SettingDump],
) -> None:
    assert by_key["CurrentDecoration"].kind == "other"


def test_an_unexpected_key_survives(by_key: dict[str, SettingDump]) -> None:
    """A key the app never named is evidence, so it is reported rather than dropped."""
    assert by_key["UnknownFromFirmware"].value == 3


def test_a_level_missing_its_bounds_is_malformed() -> None:
    described = describe_settings({"LedBrightness": {"value": 3}})
    assert {dump.key: dump.kind for dump in described}["LedBrightness"] == "malformed"


LED_CASES: dict[str, dict[str, object]] = {
    "no-document": {},
    "key-absent": {k: v for k, v in WORKING_SETTINGS.items() if k != "LedBrightness"},
    "zero-max": {
        **WORKING_SETTINGS,
        "LedBrightness": {"min": 0, "max": 0, "value": 0},
    },
    "malformed": {**WORKING_SETTINGS, "LedBrightness": 4},
    "range-off": {
        **WORKING_SETTINGS,
        "LedBrightness": {"min": 0, "max": 20, "value": 0},
    },
    "range-on": WORKING_SETTINGS,
}


@pytest.mark.parametrize(("verdict", "document"), LED_CASES.items())
def test_each_led_verdict_is_reached_and_explains_itself(
    verdict: str, document: dict[str, object]
) -> None:
    """Every verdict has to be reachable, and none may be a bare label.

    The verdict is the whole point of the tool: it is what separates a unit
    whose firmware does not model LEDs from one that has them switched off.
    """
    finding = analyse_leds(document)
    assert finding.verdict == verdict
    assert finding.detail


def test_sound_only_is_reported_as_a_gate() -> None:
    """Audio-only mode may darken the panel by design, so it cannot be silent."""
    finding = analyse_leds({**WORKING_SETTINGS, "SoundOnly": True})
    assert any("audio-only" in gate for gate in finding.gates)


def test_the_decoration_is_reported_as_a_gate() -> None:
    """Whether a decoration can hold the LEDs off is unverified, so it is reported."""
    finding = analyse_leds(WORKING_SETTINGS)
    assert any(gate.startswith("CurrentDecoration is 3") for gate in finding.gates)


def test_an_empty_document_has_no_gates() -> None:
    """With nothing read, there is nothing to say about what might gate the LEDs."""
    assert analyse_leds({}).gates == []


@pytest.fixture
def working_window() -> FakeWindow:
    return FakeWindow()


@pytest.fixture
async def working_dump(working_window: FakeWindow) -> WindowDump:
    dump = await diag.collect_dump(
        working_window, Target(address="AA:BB:CC:DD:EE:FF", name="Studio")
    )
    await working_window.drain()
    return dump


def test_both_vendor_services_are_enumerated(working_dump: WindowDump) -> None:
    """The second service is unknown to the app, so a dump that misses it is useless."""
    assert len(working_dump.services) == 2
    uuids = [service.uuid for service in working_dump.services]
    assert uuids.count(SECOND_SERVICE_UUID) == 1


def test_the_identity_characteristic_yields_a_uuid_and_a_name(
    working_dump: WindowDump,
) -> None:
    assert working_dump.device_uuid is not None
    assert working_dump.device_uuid[:8] == "0f8c1d3a"
    assert working_dump.device_name == "Studio"


def test_the_negotiated_mtu_is_recorded(working_dump: WindowDump) -> None:
    assert working_dump.mtu_negotiated == 128


def test_quick_settings_come_from_a_plain_read(working_dump: WindowDump) -> None:
    """A readable document needs no provoking, and the report says which it was."""
    assert working_dump.quick_settings_source == ["read"]


def test_a_working_window_reads_as_range_on(working_dump: WindowDump) -> None:
    assert working_dump.led is not None
    assert working_dump.led.verdict == "range-on"


@pytest.mark.usefixtures("working_dump")
def test_a_dump_writes_nothing_unless_asked(working_window: FakeWindow) -> None:
    """Reads are unconditional; a write is not, and the tool documents that."""
    assert working_window.writes == []


def _characteristics(dump: WindowDump) -> list[CharacteristicDump]:
    return [
        characteristic
        for service in dump.services
        for characteristic in service.characteristics
    ]


def test_known_characteristics_are_labelled(working_dump: WindowDump) -> None:
    """A bare UUID table explains nothing, so the recovered labels are attached."""
    labelled = [c for c in _characteristics(working_dump) if c.label]
    assert len(labelled) >= 6


def test_every_characteristic_declaring_read_answers_the_read(
    working_dump: WindowDump,
) -> None:
    failures = [
        c.uuid
        for c in _characteristics(working_dump)
        if c.value is not None and c.value.error
    ]
    assert failures == []


def test_a_write_only_characteristic_is_not_read(
    working_dump: WindowDump,
) -> None:
    """Reading the command characteristic would be an error the report invented."""
    command = [
        c for c in _characteristics(working_dump) if c.uuid == protocol.COMMAND_UUID
    ][0]
    assert command.value is None


def test_descriptors_are_enumerated_and_read(working_dump: WindowDump) -> None:
    descriptors = [
        descriptor
        for characteristic in _characteristics(working_dump)
        for descriptor in characteristic.descriptors
    ]
    assert len(descriptors) == 1
    assert descriptors[0].value is not None
    assert descriptors[0].value.hex == "01 00"


async def test_a_backend_with_no_mtu_hook_reports_its_size() -> None:
    assert await diag.negotiated_mtu(FakeWindow()) == 128


async def test_a_bluez_backend_is_nudged_past_the_23_byte_floor() -> None:
    """BlueZ reports the floor until something acquires the write descriptor.

    A dump that recorded 23 would look like a fault that is not there.
    """

    class BlueZish:
        """A backend that only reports the real MTU once it is acquired."""

        def __init__(self) -> None:
            self.acquired = False

        async def _acquire_mtu(self) -> None:
            self.acquired = True

    class Nudgeable:
        def __init__(self) -> None:
            self._backend = BlueZish()

        @property
        def mtu_size(self) -> int:
            return 128 if self._backend.acquired else 23

    assert await diag.negotiated_mtu(Nudgeable()) == 128


async def test_a_backend_that_raises_while_being_nudged_is_tolerated() -> None:
    class Broken:
        _backend = object()
        mtu_size = 64

    assert await diag.negotiated_mtu(Broken()) == 64


async def test_a_window_without_the_led_key_is_diagnosed_and_listed() -> None:
    """No write helps a firmware that does not model LEDs, so say so plainly."""
    settings = {k: v for k, v in WORKING_SETTINGS.items() if k != "LedBrightness"}
    window = FakeWindow(settings)
    dump = await diag.collect_dump(window, Target(address="AA:BB:CC:DD:EE:00"))
    await window.drain()

    assert dump.led is not None
    assert dump.led.verdict == "key-absent"
    absent = [setting.key for setting in dump.settings if setting.kind == "absent"]
    assert absent == ["LedBrightness"]


async def test_an_unreadable_document_leaves_the_led_question_open() -> None:
    """An ATT read refusal is not evidence about the LEDs either way."""
    window = FakeWindow(readable_settings=False)
    dump = await diag.collect_dump(window, Target(address="AA:BB:CC:DD:EE:01"))
    await window.drain()

    assert dump.led is not None
    assert dump.led.verdict == "no-document"


async def test_provoking_recovers_a_document_the_window_will_not_hand_over() -> None:
    """The app's C token makes the window announce its state on the notify channel.

    The announcement arrives split across notifications, so this also covers
    reassembly.
    """
    window = FakeWindow(readable_settings=False)
    dump = await diag.collect_dump(
        window,
        Target(address="AA:BB:CC:DD:EE:01"),
        provoke=True,
        echo_timeout=1.0,
    )
    await window.drain()

    assert dump.quick_settings_source == ["notify"]
    assert dump.led is not None
    assert dump.led.verdict == "range-on"
    assert window.writes[0][1] == b"C"
    assert dump.notifications >= 1


async def test_a_matched_echo_is_recognised_and_the_old_value_restored() -> None:
    """The echo is the only proof the write path works, so it is read closely."""
    window = FakeWindow()
    dump = await diag.collect_dump(
        window,
        Target(address="AA:BB:CC:DD:EE:02"),
        writes=[("LedBrightness", 9)],
        echo_timeout=1.0,
    )
    await window.drain()

    test = dump.writes[0]
    assert test.verdict == "echo-matched"
    assert test.echoed_value == 9
    assert test.previous == 7
    assert test.restored
    assert json.loads(window.writes[-1][1].decode()) == {"LedBrightness": 7}


async def test_a_clamped_echo_is_recognised() -> None:
    """A window that accepts the write and reports another value has clamped it."""
    window = FakeWindow(echo="clamp", clamp_to=0)
    dump = await diag.collect_dump(
        window,
        Target(address="AA:BB:CC:DD:EE:03"),
        writes=[("LedBrightness", 9)],
        echo_timeout=1.0,
    )
    await window.drain()
    assert dump.writes[0].verdict == "echo-clamped"


async def test_a_silent_window_is_recognised() -> None:
    window = FakeWindow(echo="none")
    dump = await diag.collect_dump(
        window,
        Target(address="AA:BB:CC:DD:EE:04"),
        writes=[("LedBrightness", 9)],
        echo_timeout=0.2,
    )
    await window.drain()
    assert dump.writes[0].verdict == "no-echo"


async def test_a_refused_write_is_recognised_and_records_the_error() -> None:
    window = FakeWindow(echo="reject")
    dump = await diag.collect_dump(
        window,
        Target(address="AA:BB:CC:DD:EE:05"),
        writes=[("LedBrightness", 9)],
        echo_timeout=0.2,
    )
    await window.drain()
    assert dump.writes[0].verdict == "write-rejected"
    assert dump.writes[0].error


@pytest.fixture
async def studio_dump() -> WindowDump:
    return await diag.collect_dump(
        FakeWindow(),
        Target(address="AA:BB:CC:DD:EE:06", name="Studio", rssi=-58),
    )


@pytest.fixture
async def bedroom_dump() -> WindowDump:
    """A second unit of the same model, differing only in its identity."""
    window = FakeWindow()
    identity = window.services[0].characteristics[0]
    identity.value = b"11112222-3333-4444-5555-666677778888,Bedroom"
    return await diag.collect_dump(
        window,
        Target(address="11:22:33:44:55:66", name="Bedroom", rssi=-71),
    )


@pytest.fixture
def normalized_studio_report(studio_dump: WindowDump) -> str:
    return format_dump(normalize(studio_dump))


def test_raw_reports_of_two_units_differ(
    studio_dump: WindowDump, bedroom_dump: WindowDump
) -> None:
    """Without this, the normalized comparison below would pass on nothing."""
    assert format_dump(studio_dump) != format_dump(bedroom_dump)


def test_normalized_reports_of_identical_units_match(
    studio_dump: WindowDump, bedroom_dump: WindowDump
) -> None:
    """Diffing two units is the fastest way to separate a broken one from a good one."""
    assert format_dump(normalize(studio_dump)) == format_dump(normalize(bedroom_dump))


def test_normalization_masks_the_address(normalized_studio_report: str) -> None:
    assert MASK in normalized_studio_report


@pytest.mark.parametrize(
    "leaked", ["AA:BB:CC:DD:EE:06", "0f8c1d3a", "Studio", "-58 dBm"]
)
def test_normalization_removes_identifying_values(
    normalized_studio_report: str, leaked: str
) -> None:
    """A normalized dump is the one meant to be attached to a public issue."""
    assert leaked not in normalized_studio_report


def test_the_report_warns_before_sharing() -> None:
    """An unnormalized dump carries device identity into a public repository."""
    header = report_header()
    assert "WARNING" in header
    assert "public" in header


def test_the_filename_stem_uses_the_stable_identity(
    studio_dump: WindowDump,
) -> None:
    assert slug(studio_dump)[:8] == "0f8c1d3a"


def test_two_units_get_two_filenames(
    studio_dump: WindowDump, bedroom_dump: WindowDump
) -> None:
    assert slug(studio_dump) != slug(bedroom_dump)


def test_a_masked_dump_has_no_identity_to_name_a_file_after(
    studio_dump: WindowDump,
) -> None:
    """So --out has to name the file from the unmasked dump.

    Otherwise every normalized dump of a multi-window run collides on one path.
    """
    assert slug(normalize(studio_dump)) == "window"


@pytest.mark.parametrize(
    "argv",
    [
        ["scan"],
        ["scan", "--unfiltered"],
        ["--json", "--normalize", "--adapter", "hci0", "--scan-timeout", "3", "scan"],
        ["dump"],
        ["dump", "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"],
        ["dump", "--name", "Studio", "--max", "2", "--timeout", "5"],
        ["dump", "--provoke", "--echo-timeout", "2", "--no-descriptors"],
        ["dump", "--led-write", "5", "--no-restore"],
        ["dump", "--write", "ScreenBrightness=6", "--write", "SoundOnly=false"],
        ["--json", "dump", "--out", "/tmp/dumps"],
    ],
)
def test_the_documented_invocations_parse(argv: list[str]) -> None:
    """Every form docs/DIAGNOSTICS.md tells an operator to run."""
    diag.build_parser().parse_args(argv)


def test_a_level_write_is_typed_as_an_integer() -> None:
    assert diag.parse_write_argument("LedBrightness=4") == ("LedBrightness", 4)


def test_a_boolean_write_is_typed_as_a_boolean() -> None:
    assert diag.parse_write_argument("SoundOnly=true") == ("SoundOnly", True)


@pytest.mark.parametrize("bad", ["LedBrightness", "NotASetting=1"])
def test_a_malformed_or_unknown_write_is_rejected(bad: str) -> None:
    """A typo must not reach the window as a write of something else."""
    with pytest.raises(argparse.ArgumentTypeError):
        diag.parse_write_argument(bad)


def test_every_characteristic_the_protocol_names_is_labelled_in_a_dump() -> None:
    """A newly discovered characteristic must not go unlabelled in a report.

    `KNOWN_CHARACTERISTICS` is deliberately a superset of what the protocol
    layer declares: it also carries the UUIDs only ever reported on hardware.
    So the check runs one way, from the protocol constants into the labels.
    """
    named = {
        name: uuid
        for name, uuid in vars(protocol).items()
        # SERVICE_UUID names a service, not a characteristic, and is labelled
        # in SERVICE_LABELS instead.
        if name.endswith("_UUID") and name != "SERVICE_UUID"
    }
    assert named
    unlabelled = [
        name for name, uuid in named.items() if uuid not in KNOWN_CHARACTERISTICS
    ]
    assert unlabelled == []
    assert protocol.SERVICE_UUID in SERVICE_LABELS
