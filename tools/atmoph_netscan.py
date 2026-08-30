#!/usr/bin/env python3
"""Unprivileged LAN reconnaissance for Atmoph Window devices.

Answers one question: does an Atmoph Window expose a network control surface,
or is BLE the only way in?

Everything here runs without root. Liveness comes from TCP connect semantics
rather than ICMP, because an unprivileged process cannot open a raw socket:

    connect() -> success        port open, host alive
    connect() -> ECONNREFUSED   port closed, host alive
    connect() -> timeout        filtered, or host absent

Subcommands:
    local                  report the interface address and derived /24
    sweep [CIDR]           find live hosts by probing a small port set
    mdns                   enumerate mDNS service types and instances
    ssdp                   M-SEARCH for UPnP/DLNA roots
    ports HOST             full 1-65535 TCP connect scan
    fingerprint HOST PORT  banner grab, then an HTTP request if it looks HTTP
    android HOST           look for Android diagnostic ports, ADB included

Results print as text and, with --json, as a machine-readable document.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import ipaddress
import json
import random
import socket
import struct
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

# Ports worth trying first on an Android-based appliance: web UIs, ADB, cast
# and DLNA endpoints, debug bridges, and the common alternates.
PROBE_PORTS: tuple[int, ...] = (
    80,
    443,
    8080,
    8443,
    5555,
    8008,
    8009,
    1900,
    7000,
    5000,
    22,
    23,
    9100,
    49152,
)

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900

# The Window 2 runs Android on a Cortex-A53, so 5555 is the prize: an open
# adbd yields `logcat` and `dumpsys wifi`, which would explain a Wi-Fi or LED
# fault without anyone reverse engineering the firmware.
ANDROID_PORTS: tuple[tuple[int, str], ...] = (
    (5555, "adbd, the on-device Android Debug Bridge"),
    (5037, "adb server, normally a host rather than a device"),
    (2323, "Fully Kiosk Browser remote administration"),
    (8080, "HTTP alternate, a kiosk or debug web UI"),
    (80, "HTTP"),
    (443, "HTTPS"),
    (5900, "VNC, sometimes present on kiosk builds"),
)

# ADB transport framing. The header is six little-endian words: command, two
# arguments, payload length, a plain byte sum of the payload, and the command
# with every bit flipped.
ADB_CNXN = 0x4E584E43
ADB_AUTH = 0x48545541
ADB_VERSION = 0x01000000
ADB_MAX_PAYLOAD = 256 * 1024
ADB_COMMANDS = {
    ADB_CNXN: "CNXN",
    ADB_AUTH: "AUTH",
    0x45534C43: "CLSE",
    0x4E45504F: "OPEN",
    0x59414B4F: "OKAY",
    0x45544257: "WRTE",
    0x53544C53: "STLS",
}

# Ports where a listener that is not adbd is itself worth reporting.
ADB_PORTS = frozenset({5555, 5037})
ADB_SPEAKING = frozenset({"adb-open", "adb-auth-required", "adb-unexpected"})


@dataclass
class PortResult:
    """Outcome of a single TCP connect attempt."""

    port: int
    state: str  # open | closed | filtered
    banner: str | None = None


@dataclass
class HostResult:
    """Everything learned about one address."""

    address: str
    alive: bool
    hostname: str | None = None
    open_ports: list[int] = field(default_factory=list)
    evidence: str = ""


@dataclass
class SurfaceResult:
    """One candidate diagnostic port and what it turned out to be."""

    port: int
    label: str
    state: str
    adb: str | None = None
    detail: str | None = None
    next_step: str | None = None


async def _connect(host: str, port: int, timeout: float) -> str:
    """Classify a single TCP connect attempt."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except TimeoutError:
        return "filtered"
    except OSError as exc:
        # A refusal proves something answered, which is what liveness needs.
        if exc.errno in {errno.ECONNREFUSED, errno.ECONNRESET}:
            return "closed"
        if exc.errno in {errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ENETUNREACH}:
            return "absent"
        return "filtered"
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return "open"


def local_ipv4() -> str | None:
    """Return this machine's primary IPv4 without reading the route table.

    A UDP socket needs no packets sent to pick a source address, so this works
    inside a sandbox that blocks the routing sysctls.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, guaranteed unrouted
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def reverse_lookup(address: str) -> str | None:
    """Resolve an address to a name, tolerating the usual failures."""
    try:
        return socket.gethostbyaddr(address)[0]
    except (OSError, socket.herror):
        return None


async def _gated_connect(
    semaphore: asyncio.Semaphore, host: str, port: int, timeout: float
) -> str:
    """Connect while holding a slot, so the file-descriptor ceiling is respected."""
    async with semaphore:
        return await _connect(host, port, timeout)


async def sweep(
    network: ipaddress.IPv4Network, timeout: float, limit: int
) -> list[HostResult]:
    """Find live hosts by probing PROBE_PORTS on every address."""
    semaphore = asyncio.Semaphore(limit)

    async def probe(address: ipaddress.IPv4Address) -> HostResult | None:
        text = str(address)
        states = await asyncio.gather(
            *(_gated_connect(semaphore, text, port, timeout) for port in PROBE_PORTS)
        )
        open_ports = [
            p for p, s in zip(PROBE_PORTS, states, strict=True) if s == "open"
        ]
        refused = any(s == "closed" for s in states)
        if not open_ports and not refused:
            return None
        return HostResult(
            address=text,
            alive=True,
            hostname=reverse_lookup(text),
            open_ports=open_ports,
            evidence="open port" if open_ports else "connection refused",
        )

    results = await asyncio.gather(*(probe(a) for a in network.hosts()))
    return sorted(
        (r for r in results if r is not None),
        key=lambda r: ipaddress.IPv4Address(r.address),
    )


async def scan_ports(
    host: str, ports: range, timeout: float, limit: int
) -> list[PortResult]:
    """TCP connect scan a port range, returning only the open ports."""
    semaphore = asyncio.Semaphore(limit)

    async def probe(port: int) -> PortResult | None:
        state = await _gated_connect(semaphore, host, port, timeout)
        return PortResult(port=port, state=state) if state == "open" else None

    results = await asyncio.gather(*(probe(p) for p in ports))
    return [r for r in results if r is not None]


def _encode_dns_name(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip(".").split("."):
        encoded = label.encode()
        out.append(len(encoded))
        out += encoded
    out.append(0)
    return bytes(out)


def _decode_dns_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    end = offset
    guard = 0
    while guard < 128:
        guard += 1
        if offset >= len(data):
            break
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                end = offset
            break
        if length & 0xC0 == 0xC0:  # compression pointer
            pointer = struct.unpack_from("!H", data, offset)[0] & 0x3FFF
            if not jumped:
                end = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        labels.append(data[offset : offset + length].decode("utf-8", "replace"))
        offset += length
        if not jumped:
            end = offset
    return ".".join(labels), end


def _mdns_query(names: list[str], qtype: int = 12) -> bytes:
    """Build a one-shot multicast DNS query for the given names (PTR by default).

    The top bit of the class field is the QU ("unicast response requested")
    bit. Without it, responders answer to the multicast group and a socket
    that has not joined the group and bound port 5353 hears nothing.
    """
    header = struct.pack("!HHHHHH", random.randrange(0, 0xFFFF), 0, len(names), 0, 0, 0)
    qclass = 0x8001
    body = b"".join(
        _encode_dns_name(n) + struct.pack("!HH", qtype, qclass) for n in names
    )
    return header + body


def _parse_mdns(data: bytes) -> list[tuple[str, str]]:
    """Extract (record name, PTR target) pairs from an mDNS response."""
    if len(data) < 12:
        return []
    _, _, qd, an, ns, ar = struct.unpack_from("!HHHHHH", data, 0)
    offset = 12
    for _ in range(qd):
        _, offset = _decode_dns_name(data, offset)
        offset += 4
    pairs: list[tuple[str, str]] = []
    for _ in range(an + ns + ar):
        if offset >= len(data):
            break
        name, offset = _decode_dns_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _, _, rdlength = struct.unpack_from("!HHIH", data, offset)
        offset += 10
        rdata = data[offset : offset + rdlength]
        if rtype == 12:  # PTR
            target, _ = _decode_dns_name(data, offset)
            pairs.append((name, target))
        elif rtype == 1 and rdlength == 4:  # A
            pairs.append((name, socket.inet_ntoa(rdata)))
        elif rtype == 16:  # TXT
            pairs.append((name, rdata.decode("utf-8", "replace")))
        offset += rdlength
    return pairs


def _mdns_socket() -> socket.socket:
    """Open a socket that hears both unicast and multicast mDNS replies.

    Binding port 5353 alongside the operating system's own responder needs
    SO_REUSEPORT. If that is refused, fall back to an ephemeral port, which
    still receives the unicast replies the QU bit asks for.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    with contextlib.suppress(AttributeError, OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        sock.bind(("", MDNS_PORT))
        membership = struct.pack(
            "4s4s", socket.inet_aton(MDNS_GROUP), socket.inet_aton("0.0.0.0")
        )
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    except OSError:
        sock.bind(("", 0))
    sock.settimeout(0.4)
    return sock


def mdns_discover(duration: float) -> dict[str, list[str]]:
    """Enumerate service types, then ask for instances of each one found."""
    sock = _mdns_socket()
    found: dict[str, list[str]] = {}
    try:
        sock.sendto(
            _mdns_query(["_services._dns-sd._udp.local"]), (MDNS_GROUP, MDNS_PORT)
        )
        deadline = time.monotonic() + duration
        asked: set[str] = set()
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(9000)
            except TimeoutError:
                # Re-ask for any newly seen service type.
                pending = [t for t in list(found) if t not in asked][:20]
                if pending:
                    asked.update(pending)
                    sock.sendto(_mdns_query(pending), (MDNS_GROUP, MDNS_PORT))
                continue
            except OSError:
                break
            for name, target in _parse_mdns(data):
                key = target if name.startswith("_services") else name
                entries = found.setdefault(key, [])
                if not name.startswith("_services") and target not in entries:
                    entries.append(target)
    finally:
        sock.close()
    return found


def ssdp_discover(duration: float) -> list[str]:
    """Send an SSDP M-SEARCH and collect the raw responses."""
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: ssdp:all\r\n"
        "\r\n"
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    replies: list[str] = []
    try:
        sock.sendto(message, (SSDP_GROUP, SSDP_PORT))
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                break
            replies.append(f"{addr[0]}\n{data.decode('utf-8', 'replace').strip()}")
    finally:
        sock.close()
    return replies


async def fingerprint(host: str, port: int, timeout: float) -> dict[str, str]:
    """Grab whatever a port volunteers, then try HTTP if nothing arrives."""
    out: dict[str, str] = {"host": host, "port": str(port)}
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (TimeoutError, OSError) as exc:
        out["error"] = str(exc)
        return out
    try:
        with contextlib.suppress(TimeoutError):
            greeting = await asyncio.wait_for(reader.read(512), timeout=2.0)
            if greeting:
                out["banner"] = greeting.decode("utf-8", "replace").strip()
        if "banner" not in out:
            writer.write(
                f"GET / HTTP/1.1\r\nHost: {host}\r\n"
                "User-Agent: atmoph-netscan\r\nConnection: close\r\n\r\n".encode()
            )
            await writer.drain()
            with contextlib.suppress(TimeoutError):
                body = await asyncio.wait_for(reader.read(4096), timeout=timeout)
                if body:
                    out["http"] = body.decode("utf-8", "replace")[:2000].strip()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return out


def _adb_message(command: int, arg0: int, arg1: int, payload: bytes) -> bytes:
    """Frame one ADB transport message."""
    header = struct.pack(
        "<6I",
        command,
        arg0,
        arg1,
        len(payload),
        sum(payload) & 0xFFFFFFFF,
        command ^ 0xFFFFFFFF,
    )
    return header + payload


async def adb_probe(host: str, port: int, timeout: float) -> dict[str, str]:
    """Ask a listener to complete the ADB connect handshake.

    This separates a real adbd from anything else that happens to listen on
    5555, and separates a device that will accept a connection from one that
    will first demand the authorisation prompt. Nothing is run on the device:
    the exchange stops at the banner adbd sends unasked.
    """
    out: dict[str, str] = {}
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (TimeoutError, OSError) as exc:
        out["state"] = "unreachable"
        out["detail"] = str(exc)
        return out
    try:
        writer.write(
            _adb_message(ADB_CNXN, ADB_VERSION, ADB_MAX_PAYLOAD, b"host::atmoph\x00")
        )
        await writer.drain()
        try:
            header = await asyncio.wait_for(reader.readexactly(24), timeout=timeout)
        except (TimeoutError, asyncio.IncompleteReadError):
            out["state"] = "no-reply"
            out["detail"] = "nothing answered the ADB handshake within the timeout"
            return out

        command, arg0, _arg1, length, _check, magic = struct.unpack("<6I", header)
        if magic != command ^ 0xFFFFFFFF:
            out["state"] = "not-adb"
            out["detail"] = f"reply is not ADB framing: {header.hex(' ')}"
            return out

        payload = b""
        if 0 < length <= ADB_MAX_PAYLOAD:
            with contextlib.suppress(TimeoutError, asyncio.IncompleteReadError):
                payload = await asyncio.wait_for(
                    reader.readexactly(length), timeout=timeout
                )
        out["reply"] = ADB_COMMANDS.get(command, f"0x{command:08x}")

        if command == ADB_CNXN:
            out["state"] = "adb-open"
            out["banner"] = payload.rstrip(b"\x00").decode("utf-8", "replace")
        elif command == ADB_AUTH:
            out["state"] = "adb-auth-required"
            out["detail"] = (
                f"adbd sent an auth token (type {arg0}), so it needs this host's "
                "key authorised on the device first"
            )
        else:
            out["state"] = "adb-unexpected"
            out["detail"] = f"ADB framing but a {out['reply']} reply"
        return out
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _next_step(host: str, port: int, adb: str | None) -> str | None:
    """Say what the operator should do about an open port."""
    if adb in ADB_SPEAKING:
        prompt = (
            " The window has a screen, so watch it for the authorisation prompt."
            if adb == "adb-auth-required"
            else ""
        )
        return (
            f"adb connect {host}:{port}, then `adb -s {host}:{port} logcat` and "
            f"`adb -s {host}:{port} shell dumpsys wifi`.{prompt}"
        )
    if port in ADB_PORTS:
        return (
            f"something listens on {port} but does not speak ADB; try "
            f"fingerprint {host} {port}"
        )
    if port == 2323:
        return f"open http://{host}:2323 for the Fully Kiosk admin page"
    if port == 5900:
        return f"point a VNC viewer at {host}:5900"
    return f"fingerprint {host} {port}"


async def android_surfaces(
    host: str, ports: Sequence[tuple[int, str]], timeout: float, limit: int
) -> list[SurfaceResult]:
    """Check one host for the Android diagnostic ports worth having.

    Every open port gets an ADB handshake, not only 5555, because adbd on a
    non-default port is exactly the sort of thing this is looking for. The
    result is recorded separately from the TCP state so a listener that simply
    is not adbd does not read as a failure.
    """
    semaphore = asyncio.Semaphore(limit)
    states = await asyncio.gather(
        *(_gated_connect(semaphore, host, port, timeout) for port, _ in ports)
    )
    results: list[SurfaceResult] = []
    for (port, label), state in zip(ports, states, strict=True):
        result = SurfaceResult(port=port, label=label, state=state)
        if state == "open":
            probe = await adb_probe(host, port, timeout)
            verdict = probe.get("state", "not-adb")
            if verdict in ADB_SPEAKING or port in ADB_PORTS:
                result.adb = verdict
                result.detail = probe.get("banner") or probe.get("detail")
            result.next_step = _next_step(host, port, result.adb)
        results.append(result)
    return results


def _emit(payload: object, as_json: bool, text: str) -> None:
    print(json.dumps(payload, indent=2) if as_json else text)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable output"
    )
    parser.add_argument(
        "--timeout", type=float, default=1.0, help="per-connect timeout"
    )
    parser.add_argument(
        "--concurrency", type=int, default=512, help="in-flight sockets"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("local", help="report interface address and derived /24")
    p_sweep = sub.add_parser("sweep", help="find live hosts")
    p_sweep.add_argument("cidr", nargs="?", help="defaults to the local /24")
    p_mdns = sub.add_parser("mdns", help="enumerate mDNS services")
    p_mdns.add_argument("--duration", type=float, default=8.0)
    p_ssdp = sub.add_parser("ssdp", help="M-SEARCH for UPnP roots")
    p_ssdp.add_argument("--duration", type=float, default=6.0)
    p_ports = sub.add_parser("ports", help="full TCP connect scan")
    p_ports.add_argument("host")
    p_ports.add_argument("--first", type=int, default=1)
    p_ports.add_argument("--last", type=int, default=65535)
    p_fp = sub.add_parser("fingerprint", help="banner grab and HTTP probe")
    p_fp.add_argument("host")
    p_fp.add_argument("port", type=int)
    p_android = sub.add_parser(
        "android", help="look for Android diagnostic ports, ADB included"
    )
    p_android.add_argument("host")
    p_android.add_argument(
        "--ports",
        help="comma-separated port list, overriding the built-in Android set",
    )

    args = parser.parse_args()

    if args.command == "local":
        address = local_ipv4()
        if address is None:
            print("could not determine a local IPv4 address", file=sys.stderr)
            return 1
        network = ipaddress.ip_network(f"{address}/24", strict=False)
        _emit(
            {"address": address, "network": str(network)},
            args.json,
            f"address {address}\nnetwork {network}",
        )
        return 0

    if args.command == "sweep":
        cidr = args.cidr
        if cidr is None:
            address = local_ipv4()
            if address is None:
                print("could not determine the local network", file=sys.stderr)
                return 1
            cidr = f"{address}/24"
        network = ipaddress.ip_network(cidr, strict=False)
        started = time.monotonic()
        hosts = await sweep(network, args.timeout, args.concurrency)
        elapsed = time.monotonic() - started
        lines = [f"{len(hosts)} live host(s) on {network} in {elapsed:.1f}s"]
        for host in hosts:
            ports = ",".join(str(p) for p in host.open_ports) or "-"
            lines.append(f"  {host.address:<16} {host.hostname or '-':<32} {ports}")
        _emit([asdict(h) for h in hosts], args.json, "\n".join(lines))
        return 0

    if args.command == "mdns":
        services = mdns_discover(args.duration)
        lines = [f"{len(services)} mDNS name(s)"]
        for name in sorted(services):
            targets = ", ".join(services[name]) or "-"
            lines.append(f"  {name} -> {targets}")
        _emit(services, args.json, "\n".join(lines))
        return 0

    if args.command == "ssdp":
        replies = ssdp_discover(args.duration)
        _emit(
            replies,
            args.json,
            f"{len(replies)} SSDP reply/replies\n" + "\n---\n".join(replies),
        )
        return 0

    if args.command == "ports":
        started = time.monotonic()
        results = await scan_ports(
            args.host, range(args.first, args.last + 1), args.timeout, args.concurrency
        )
        elapsed = time.monotonic() - started
        lines = [
            f"{len(results)} open port(s) on {args.host} "
            f"({args.first}-{args.last} in {elapsed:.1f}s)"
        ]
        lines += [f"  {r.port}/tcp open" for r in results]
        _emit([asdict(r) for r in results], args.json, "\n".join(lines))
        return 0

    if args.command == "fingerprint":
        result = await fingerprint(args.host, args.port, args.timeout)
        _emit(
            result,
            args.json,
            "\n".join(f"{k}: {v}" for k, v in result.items()),
        )
        return 0

    if args.command == "android":
        if args.ports:
            ports = tuple(
                (int(p.strip()), "requested")
                for p in args.ports.split(",")
                if p.strip()
            )
        else:
            ports = ANDROID_PORTS
        surfaces = await android_surfaces(
            args.host, ports, args.timeout, args.concurrency
        )
        answered = [s for s in surfaces if s.state != "filtered"]
        lines = [f"Android diagnostic surfaces on {args.host}"]
        for surface in surfaces:
            state = f"{surface.state}/{surface.adb}" if surface.adb else surface.state
            lines.append(f"  {surface.port:>5}/tcp {state:<22} {surface.label}")
            if surface.detail:
                lines.append(f"      {surface.detail}")
            if surface.next_step:
                lines.append(f"      next: {surface.next_step}")
        if not answered:
            lines.append(
                "\nEvery port timed out, which is not the same as closed. macOS "
                "sandboxing blocks LAN TCP connects and returns exactly this, so "
                "re-run from an unsandboxed terminal on the window's own subnet "
                "before recording that nothing is open."
            )
        _emit([asdict(s) for s in surfaces], args.json, "\n".join(lines))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
