"""Offline verification of tools/atmoph_netscan.py.

The ADB checks run against a real listening socket rather than a stubbed
`asyncio.open_connection`, because the framing and the read sizes are what can
be wrong and a handshake that never left the process would prove nothing about
either. The mDNS and SSDP discovery paths need a LAN and are not covered.

See tests/tools/fakes.py for the listener these drive.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

import atmoph_netscan as netscan
from tests.tools.fakes import FakeAdbd

# Port 1 is nothing's default, so a connect to it is refused rather than
# answered, which is the "closed" half of the surface scan.
REFUSED_PORT = 1

FRAMED_CNXN = netscan._adb_message(netscan.ADB_CNXN, 1, 2, b"host::")

StartListener = Callable[[str], Awaitable[FakeAdbd]]


@pytest.fixture
async def listener() -> AsyncIterator[StartListener]:
    """Start fake adbd listeners, closing every one when the test ends."""
    servers: list[FakeAdbd] = []

    async def start(reply: str) -> FakeAdbd:
        server = FakeAdbd(reply)
        await server.start()
        servers.append(server)
        return server

    try:
        yield start
    finally:
        for server in servers:
            await server.stop()


def test_the_command_word_and_arguments_survive_framing() -> None:
    command, arg0, arg1 = struct.unpack_from("<3I", FRAMED_CNXN, 0)
    assert command == netscan.ADB_CNXN
    assert (arg0, arg1) == (1, 2)


def test_the_payload_length_and_checksum_are_set() -> None:
    """adbd drops a message whose header does not describe its payload."""
    length, check = struct.unpack_from("<2I", FRAMED_CNXN, 12)
    assert length == 6
    assert check == sum(b"host::")


def test_the_magic_word_is_the_inverted_command() -> None:
    """The magic is how the probe tells ADB framing from anything else."""
    command, _arg0, _arg1, _length, _check, magic = struct.unpack_from(
        "<6I", FRAMED_CNXN, 0
    )
    assert magic == command ^ 0xFFFFFFFF


def test_the_payload_follows_the_header() -> None:
    assert FRAMED_CNXN[24:] == b"host::"


@pytest.mark.parametrize(
    ("reply", "state"),
    [
        ("cnxn", "adb-open"),
        ("auth", "adb-auth-required"),
        ("garbage", "not-adb"),
        ("silent", "no-reply"),
    ],
)
async def test_a_listener_is_classified_by_how_it_answers_the_handshake(
    listener: StartListener, reply: str, state: str
) -> None:
    """This separates a real adbd from anything else listening on 5555.

    It also separates a device that will accept a connection from one that
    will first demand the on-screen authorisation prompt. Every listener is
    sent a real handshake, whatever it answers with.
    """
    server = await listener(reply)
    result = await netscan.adb_probe("127.0.0.1", server.port, 0.4)

    assert result["state"] == state
    assert struct.unpack_from("<I", server.request, 0)[0] == netscan.ADB_CNXN


async def test_the_device_banner_is_captured(listener: StartListener) -> None:
    """The banner is all the probe collects, and it names the build."""
    server = await listener("cnxn")
    result = await netscan.adb_probe("127.0.0.1", server.port, 0.4)
    assert "ro.product.name" in result.get("banner", "")


async def test_a_refused_port_reads_as_unreachable() -> None:
    """A closed port is not a listener that failed the handshake."""
    probe = await netscan.adb_probe("127.0.0.1", REFUSED_PORT, 0.3)
    assert probe["state"] == "unreachable"


async def test_an_open_port_is_reported_open_and_a_refused_one_closed(
    listener: StartListener,
) -> None:
    server = await listener("cnxn")
    surfaces = await netscan.android_surfaces(
        "127.0.0.1",
        ((server.port, "fake adbd"), (REFUSED_PORT, "refused")),
        0.4,
        8,
    )

    found = {surface.port: surface for surface in surfaces}
    assert found[server.port].state == "open"
    assert found[REFUSED_PORT].state == "closed"


async def test_adbd_on_a_non_default_port_is_still_found(
    listener: StartListener,
) -> None:
    """Every open port gets the handshake, because that is what this looks for."""
    server = await listener("cnxn")
    surfaces = await netscan.android_surfaces(
        "127.0.0.1", ((server.port, "fake adbd"),), 0.4, 8
    )

    assert surfaces[0].adb == "adb-open"
    assert "ro.product.name" in (surfaces[0].detail or "")


async def test_only_an_open_port_carries_a_next_step(
    listener: StartListener,
) -> None:
    server = await listener("cnxn")
    surfaces = await netscan.android_surfaces(
        "127.0.0.1",
        ((server.port, "fake adbd"), (REFUSED_PORT, "refused")),
        0.4,
        8,
    )

    found = {surface.port: surface for surface in surfaces}
    assert found[server.port].next_step
    assert found[REFUSED_PORT].next_step is None


async def test_a_non_adb_listener_on_a_non_adb_port_is_not_labelled(
    listener: StartListener,
) -> None:
    """A listener that simply is not adbd must not read as a failed probe."""
    server = await listener("garbage")
    surfaces = await netscan.android_surfaces(
        "127.0.0.1", ((server.port, "not adbd"),), 0.4, 8
    )
    assert surfaces[0].adb is None


def test_adb_gets_the_logcat_and_dumpsys_instruction() -> None:
    assert "logcat" in (netscan._next_step("192.0.2.10", 5555, "adb-open") or "")


def test_an_auth_required_adb_mentions_the_on_screen_prompt() -> None:
    """The window has a screen, so the operator has to be told to watch it."""
    step = netscan._next_step("192.0.2.10", 5555, "adb-auth-required") or ""
    assert "prompt" in step


def test_a_non_adb_listener_on_5555_is_not_mistaken_for_adb() -> None:
    assert "does not speak ADB" in (netscan._next_step("192.0.2.10", 5555, None) or "")


def test_the_kiosk_admin_port_gets_a_url() -> None:
    assert "2323" in (netscan._next_step("192.0.2.10", 2323, None) or "")


def test_any_other_open_port_gets_a_fingerprint_suggestion() -> None:
    step = netscan._next_step("192.0.2.10", 9999, None)
    assert step == "fingerprint 192.0.2.10 9999"


def test_5555_is_in_the_built_in_port_set() -> None:
    assert 5555 in {port for port, _ in netscan.ANDROID_PORTS}
