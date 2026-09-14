#!/usr/bin/env python3
"""BLE diagnostic dump for Atmoph Window devices.

Produces the evidence needed to explain a misbehaving window from outside its
firmware: the complete GATT table, every readable value, and the quick-settings
document that reveals whether the firmware believes the unit has LEDs at all.

Several units can be dumped in one run and their reports diffed, which is the
fastest way to separate a broken window from a working one.

Subcommands:
    scan                 active-scan for windows advertising the vendor service
    dump [ADDRESS ...]   connect and dump; scans first when given no address

Reads are unconditional. No application payload is written unless --provoke,
--led-write, or --write is given. Subscribing to notifications does write a
client characteristic configuration descriptor, but that is transport state and
changes nothing on the device.

This module owns the parts that need a radio — the scan, the connect, the GATT
walk, the write test — and the command line that drives them. The dump they
produce is described and rendered by atmoph_dump.py, against the tables in
atmoph_catalog.py, neither of which knows bleak exists. Parsing and report
formatting are verified offline against a fake peripheral by
tests/tools/test_diag.py.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atmoph_catalog import (
    KNOWN_CHARACTERISTICS,
    LED_KEY,
    SCREEN_BRIGHTNESS_KEY,
    SECOND_SERVICE_UUID,
    SERVICE_LABELS,
)
from atmoph_dump import (
    CharacteristicDump,
    DescriptorDump,
    ServiceDump,
    Target,
    ValueDump,
    WindowDump,
    WriteTest,
    analyse_leds,
    describe_settings,
    format_dump,
    normalize,
    render_value,
    report_header,
    slug,
)
from atmoph_wire import (
    COMMAND_UUID,
    IDENTITY_UUID,
    QUICK_SETTINGS_UUID,
    SERVICE_UUID,
    SETTING_KEYS,
    JsonObjectStream,
    Level,
    encode_command,
    encode_setting,
)


class ScanUnavailable(RuntimeError):
    """The host refused to scan, which is not the same as finding nothing."""


def _import_bleak() -> tuple[Any, Any]:
    """Import bleak lazily so --help works on a host without it installed."""
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as err:  # pragma: no cover - environment dependent
        raise SystemExit(
            "bleak is required for scan and dump: pip install bleak"
        ) from err
    return BleakClient, BleakScanner


class SettingsWatcher:
    """Reassemble quick-settings documents arriving on the notify channel."""

    def __init__(self) -> None:
        self._stream = JsonObjectStream()
        self.merged: dict[str, object] = {}
        self.count = 0
        self._queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def feed(self, payload: bytes) -> None:
        """Accept one notification, ignoring a payload that never parses."""
        try:
            documents = self._stream.feed(payload)
        except ValueError:
            return
        for document in documents:
            self.count += 1
            self.merged.update(document)
            self._queue.put_nowait(document)

    async def wait_for_key(
        self, key: str, timeout: float
    ) -> tuple[object | None, float]:
        """Wait for the next document mentioning a key, returning its value."""
        started = time.monotonic()
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return None, time.monotonic() - started
            try:
                document = await asyncio.wait_for(self._queue.get(), remaining)
            except TimeoutError:
                return None, time.monotonic() - started
            if key in document:
                return document[key], time.monotonic() - started


async def discover(
    timeout: float, adapter: str | None, unfiltered: bool
) -> list[Target]:
    """Actively scan for windows, merging the scan response into each result.

    The advertised name rides in the scan response rather than the
    advertisement, so the same window is seen named and nameless seconds apart.
    Detections are merged per address so a name observed once is kept.
    """
    _, scanner_class = _import_bleak()
    seen: dict[str, Target] = {}

    def detected(device: Any, advertisement: Any) -> None:
        uuids = sorted({str(u).lower() for u in (advertisement.service_uuids or [])})
        matched = SERVICE_UUID in uuids
        name = device.name or advertisement.local_name or None
        target = seen.get(device.address)
        if target is None:
            if not (matched or unfiltered):
                return
            seen[device.address] = Target(
                address=device.address,
                name=name,
                rssi=advertisement.rssi,
                service_uuids=uuids,
                matched_service=matched,
                device=device,
            )
            return
        target.device = device
        target.rssi = advertisement.rssi
        target.matched_service = target.matched_service or matched
        target.service_uuids = sorted(set(target.service_uuids) | set(uuids))
        if name:
            target.name = name

    kwargs: dict[str, Any] = {"scanning_mode": "active"}
    if not unfiltered:
        kwargs["service_uuids"] = [SERVICE_UUID]
    if adapter:
        kwargs["adapter"] = adapter

    scanner = scanner_class(detection_callback=detected, **kwargs)
    try:
        await scanner.start()
    except Exception as exc:
        raise ScanUnavailable(
            f"this host will not scan ({type(exc).__name__}: {exc}). That is not "
            "the same as finding no window: macOS refuses Bluetooth to a "
            "sandboxed process, so run this from a normal terminal that has been "
            "granted the Bluetooth permission, and check the adapter is on."
        ) from exc
    try:
        await asyncio.sleep(timeout)
    finally:
        with contextlib.suppress(Exception):
            await scanner.stop()
    return sorted(seen.values(), key=lambda t: (t.name or "\uffff", t.address))


async def negotiated_mtu(client: Any) -> int | None:
    """Report the MTU actually in force, nudging BlueZ into telling the truth.

    bleak has no MTU request API, because CoreBluetooth and BlueZ both
    negotiate it themselves. The BlueZ backend then reports the 23-byte floor
    until something acquires the write file descriptor, which bleak's own
    mtu_size example works around this way. The app asks for 128, so a dump
    that recorded 23 would look like a fault that is not there.
    """
    acquire = getattr(getattr(client, "_backend", None), "_acquire_mtu", None)
    if acquire is not None:
        with contextlib.suppress(Exception):
            await acquire()
    return getattr(client, "mtu_size", None)


async def _read_characteristic(client: Any, characteristic: Any) -> ValueDump:
    try:
        raw = await client.read_gatt_char(characteristic)
    except Exception as exc:
        return ValueDump(error=f"{type(exc).__name__}: {exc}")
    return render_value(bytes(raw))


async def collect_gatt(client: Any, read_descriptors: bool) -> list[ServiceDump]:
    """Enumerate every service, characteristic, descriptor, and readable value."""
    services: list[ServiceDump] = []
    for service in client.services:
        characteristics: list[CharacteristicDump] = []
        for characteristic in service.characteristics:
            properties = sorted(characteristic.properties)
            value = None
            if "read" in properties:
                value = await _read_characteristic(client, characteristic)
            descriptors: list[DescriptorDump] = []
            for descriptor in characteristic.descriptors:
                dumped = DescriptorDump(
                    uuid=str(descriptor.uuid).lower(),
                    handle=descriptor.handle,
                    description=getattr(descriptor, "description", None) or None,
                )
                if read_descriptors:
                    try:
                        raw = await client.read_gatt_descriptor(descriptor.handle)
                    except Exception as exc:
                        dumped.value = ValueDump(error=f"{type(exc).__name__}: {exc}")
                    else:
                        dumped.value = render_value(bytes(raw))
                descriptors.append(dumped)
            uuid = str(characteristic.uuid).lower()
            characteristics.append(
                CharacteristicDump(
                    uuid=uuid,
                    handle=characteristic.handle,
                    properties=properties,
                    label=KNOWN_CHARACTERISTICS.get(uuid),
                    description=getattr(characteristic, "description", None) or None,
                    value=value,
                    descriptors=sorted(descriptors, key=lambda d: (d.uuid, d.handle)),
                )
            )
        uuid = str(service.uuid).lower()
        services.append(
            ServiceDump(
                uuid=uuid,
                handle=service.handle,
                label=SERVICE_LABELS.get(uuid),
                characteristics=sorted(
                    characteristics, key=lambda c: (c.uuid, c.handle)
                ),
            )
        )
    return sorted(services, key=_service_order)


def _service_order(service: ServiceDump) -> tuple[int, str, int]:
    """Order the two vendor services first, then sort the rest by UUID.

    Handles are not stable across firmware revisions, so a diff needs a sort
    that does not depend on them.
    """
    rank = {SERVICE_UUID: 0, SECOND_SERVICE_UUID: 1}.get(service.uuid, 2)
    return rank, service.uuid, service.handle


def find_value(services: list[ServiceDump], uuid: str) -> ValueDump | None:
    """Return the value already read for a characteristic, if it was readable."""
    for service in services:
        for characteristic in service.characteristics:
            if characteristic.uuid == uuid and characteristic.value is not None:
                return characteristic.value
    return None


def _parse_document(value: ValueDump | None) -> dict[str, object] | None:
    if value is None or not value.text:
        return None
    try:
        parsed = json.loads(value.text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _run_write_test(
    client: Any,
    watcher: SettingsWatcher,
    known: dict[str, object],
    key: str,
    value: bool | int | str,
    echo_timeout: float,
    restore: bool,
) -> WriteTest:
    """Write one setting, watch for the echo, and put the old value back.

    A setting write is echoed on the notify channel, which makes this the only
    reliable way to prove the write path works: an observed view change does
    not, because DailyRoutineEnable rotates views on its own.
    """
    previous = known.get(key)
    level = Level.from_wire(previous)
    test = WriteTest(
        key=key,
        requested=value,
        previous=level.value if level is not None else previous,
    )
    try:
        await client.write_gatt_char(
            QUICK_SETTINGS_UUID, encode_setting(key, value), response=True
        )
    except Exception as exc:
        test.error = f"{type(exc).__name__}: {exc}"
        test.verdict = "write-rejected"
        return test
    test.written = True

    echoed, elapsed = await watcher.wait_for_key(key, echo_timeout)
    test.elapsed = round(elapsed, 3)
    if echoed is None:
        test.verdict = "no-echo"
    else:
        test.echoed = True
        echoed_level = Level.from_wire(echoed)
        test.echoed_value = echoed_level.value if echoed_level else echoed
        test.verdict = "echo-matched" if test.echoed_value == value else "echo-clamped"

    if (
        restore
        and test.written
        and isinstance(test.previous, bool | int | str)
        and test.previous != value
    ):
        with contextlib.suppress(Exception):
            await client.write_gatt_char(
                QUICK_SETTINGS_UUID,
                encode_setting(key, test.previous),
                response=True,
            )
            confirmed, _ = await watcher.wait_for_key(key, echo_timeout)
            test.restored = confirmed is not None
    return test


async def collect_dump(
    client: Any,
    target: Target,
    *,
    provoke: bool = False,
    writes: list[tuple[str, bool | int | str]] | None = None,
    echo_timeout: float = 6.0,
    read_descriptors: bool = True,
    restore: bool = True,
) -> WindowDump:
    """Dump one already-connected window."""
    dump = WindowDump(
        address=target.address,
        generated=datetime.now(UTC).isoformat(timespec="seconds"),
        advertised_name=target.name,
        rssi=target.rssi,
        advertised_service_uuids=list(target.service_uuids),
        connected=bool(getattr(client, "is_connected", True)),
        mtu_negotiated=await negotiated_mtu(client),
    )

    watcher = SettingsWatcher()
    subscribed = False
    try:
        await client.start_notify(
            QUICK_SETTINGS_UUID, lambda _sender, data: watcher.feed(bytes(data))
        )
        subscribed = True
    except Exception as exc:
        dump.errors.append(f"quick-settings notify unavailable: {exc}")

    try:
        dump.services = await collect_gatt(client, read_descriptors)

        identity = find_value(dump.services, IDENTITY_UUID)
        if identity is not None and identity.text:
            parts = identity.text.split(",", 1)
            dump.device_uuid = parts[0] or None
            dump.device_name = parts[1] if len(parts) > 1 and parts[1] else None

        document = _parse_document(find_value(dump.services, QUICK_SETTINGS_UUID))
        if document is not None:
            dump.quick_settings.update(document)
            dump.quick_settings_source.append("read")

        if provoke:
            try:
                await client.write_gatt_char(
                    COMMAND_UUID, encode_command("connect_notify"), response=True
                )
            except Exception as exc:
                dump.errors.append(f"state request write failed: {exc}")
            else:
                await watcher.wait_for_key(SCREEN_BRIGHTNESS_KEY, echo_timeout)

        if watcher.merged:
            dump.quick_settings.update(watcher.merged)
            dump.quick_settings_source.append("notify")

        # The findings describe the window as it was found. A write test would
        # otherwise overwrite the evidence it exists to explain, so it reports
        # its own before and after values instead.
        found = dict(dump.quick_settings)
        for key, value in writes or []:
            found.update(watcher.merged)
            dump.writes.append(
                await _run_write_test(
                    client, watcher, found, key, value, echo_timeout, restore
                )
            )
    finally:
        if subscribed:
            with contextlib.suppress(Exception):
                await client.stop_notify(QUICK_SETTINGS_UUID)

    dump.notifications = watcher.count
    dump.settings = describe_settings(dump.quick_settings)
    dump.led = analyse_leds(dump.quick_settings)
    return dump


async def dump_window(
    target: Target,
    *,
    timeout: float,
    provoke: bool,
    writes: list[tuple[str, bool | int | str]],
    echo_timeout: float,
    read_descriptors: bool,
    restore: bool,
) -> WindowDump:
    """Connect to one window and dump it, reporting a failure as a dump."""
    client_class, _ = _import_bleak()
    client = client_class(target.device or target.address, timeout=timeout)
    try:
        await client.connect()
    except Exception as exc:
        return WindowDump(
            address=target.address,
            generated=datetime.now(UTC).isoformat(timespec="seconds"),
            advertised_name=target.name,
            rssi=target.rssi,
            advertised_service_uuids=list(target.service_uuids),
            errors=[f"connect failed: {type(exc).__name__}: {exc}"],
        )
    try:
        return await collect_dump(
            client,
            target,
            provoke=provoke,
            writes=writes,
            echo_timeout=echo_timeout,
            read_descriptors=read_descriptors,
            restore=restore,
        )
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


def parse_write_argument(text: str) -> tuple[str, bool | int | str]:
    """Parse a KEY=VALUE write request, typing the value as the app does."""
    key, separator, raw = text.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    key = key.strip()
    if key not in SETTING_KEYS:
        known = ", ".join(sorted(SETTING_KEYS))
        raise argparse.ArgumentTypeError(f"unknown setting {key!r}; known: {known}")
    raw = raw.strip()
    if raw.lower() in {"true", "false"}:
        return key, raw.lower() == "true"
    try:
        return key, int(raw)
    except ValueError:
        return key, raw


def build_parser() -> argparse.ArgumentParser:
    """Build the command line, kept close to tools/atmoph_netscan.py."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable output"
    )
    parser.add_argument(
        "--normalize",
        action="store_true",
        help="mask volatile and identity fields so dumps diff cleanly",
    )
    parser.add_argument("--adapter", help="Bluetooth adapter, BlueZ hosts only")
    parser.add_argument(
        "--scan-timeout", type=float, default=12.0, help="seconds to scan"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="active-scan for windows")
    p_scan.add_argument(
        "--unfiltered",
        action="store_true",
        help="report every advertiser, not only windows",
    )

    p_dump = sub.add_parser("dump", help="connect and dump the full GATT table")
    p_dump.add_argument("addresses", nargs="*", help="skip the scan and use these")
    p_dump.add_argument(
        "--name",
        action="append",
        default=[],
        help="only dump discovered windows with this advertised name",
    )
    p_dump.add_argument(
        "--max", type=int, default=0, help="stop after this many windows"
    )
    p_dump.add_argument(
        "--timeout", type=float, default=30.0, help="per-connect timeout"
    )
    p_dump.add_argument(
        "--echo-timeout",
        type=float,
        default=6.0,
        help="seconds to wait for a notification echo",
    )
    p_dump.add_argument(
        "--no-descriptors",
        action="store_true",
        help="enumerate descriptors without reading them",
    )
    p_dump.add_argument(
        "--hex",
        action="store_true",
        help="print the hex of every value, not only the undecodable ones",
    )
    p_dump.add_argument(
        "--provoke",
        action="store_true",
        help="write the app's C state request so the window announces settings",
    )
    p_dump.add_argument(
        "--led-write",
        type=int,
        metavar="N",
        help=f'write {{"{LED_KEY}": N}} and report whether it is echoed',
    )
    p_dump.add_argument(
        "--write",
        action="append",
        default=[],
        type=parse_write_argument,
        metavar="KEY=VALUE",
        help="write any known setting and report the echo, repeatable",
    )
    p_dump.add_argument(
        "--no-restore",
        action="store_true",
        help="leave written settings at their new value",
    )
    p_dump.add_argument("--out", help="write one report file per window into DIR")
    return parser


def _emit(payload: object, as_json: bool, text: str) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else text)


async def _command_scan(args: argparse.Namespace) -> int:
    targets = await discover(args.scan_timeout, args.adapter, args.unfiltered)
    lines = [f"{len(targets)} device(s) in {args.scan_timeout:.0f}s of active scanning"]
    for target in targets:
        marker = "window" if target.matched_service else "other"
        rssi = "-" if target.rssi is None else f"{target.rssi} dBm"
        lines.append(
            f"  {target.address:<40} {target.name or '(nameless)':<24} "
            f"{rssi:<9} {marker}"
        )
    if not targets:
        lines.append(
            "  nothing found. The name rides in the scan response, so a window "
            "can appear nameless; scan longer, close the phone app, and check "
            "that this host can actually scan before concluding it is absent."
        )
    _emit([asdict(t) | {"device": None} for t in targets], args.json, "\n".join(lines))
    return 0


async def _resolve_targets(args: argparse.Namespace) -> list[Target]:
    if args.addresses:
        return [Target(address=address) for address in args.addresses]
    targets = [
        target
        for target in await discover(args.scan_timeout, args.adapter, False)
        if target.matched_service
    ]
    if args.name:
        wanted = {name.lower() for name in args.name}
        targets = [t for t in targets if (t.name or "").lower() in wanted]
    if args.max > 0:
        targets = targets[: args.max]
    return targets


async def _command_dump(args: argparse.Namespace) -> int:
    writes: list[tuple[str, bool | int | str]] = list(args.write)
    if args.led_write is not None:
        writes.insert(0, (LED_KEY, args.led_write))

    targets = await _resolve_targets(args)
    if not targets:
        print(
            "no window found. Scan first with the scan subcommand, or pass an "
            "address explicitly. Addresses rotate, so a stale one will fail.",
            file=sys.stderr,
        )
        return 1

    # The filename has to come from the real identity even when the contents
    # are masked, or every normalized dump would land on the same path.
    dumps: list[tuple[WindowDump, WindowDump]] = []
    for target in targets:
        dump = await dump_window(
            target,
            timeout=args.timeout,
            provoke=args.provoke,
            writes=writes,
            echo_timeout=args.echo_timeout,
            read_descriptors=not args.no_descriptors,
            restore=not args.no_restore,
        )
        dumps.append((dump, normalize(dump) if args.normalize else dump))

    if args.out:
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        suffix = "json" if args.json else "txt"
        for identity, dump in dumps:
            body = (
                json.dumps(asdict(dump), indent=2, sort_keys=True)
                if args.json
                else f"{report_header()}\n\n{format_dump(dump, args.hex)}"
            )
            path = directory / f"{slug(identity)}.{suffix}"
            path.write_text(f"{body}\n", encoding="utf-8")
            print(f"wrote {path}", file=sys.stderr)

    presented = [dump for _, dump in dumps]
    text = "\n\n".join(
        [report_header()] + [format_dump(dump, args.hex) for dump in presented]
    )
    _emit([asdict(dump) for dump in presented], args.json, text)
    return 0 if any(dump.connected for dump in presented) else 1


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            return await _command_scan(args)
        if args.command == "dump":
            return await _command_dump(args)
    except ScanUnavailable as err:
        print(err, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
