# Atmoph Window 2 Remote

Local Bluetooth Low Energy control for Atmoph Window 2 through Home Assistant.
The integration reproduces the official Android app's GATT operations without
requiring Atmoph cloud access.

## Status

The Android app protocol has been statically verified against Atmoph Window 2
app 2.3.4 (`com.atmoph.remote`, version code 2030400). The offline protocol
tests pass against a simulated peripheral. Hardware verification against a
Window 2 is still required before the first release.

## Supported controls

- Automatic Bluetooth discovery and UI setup
- Display wake/sleep with read-before-toggle state confirmation
- Current view title, location, image URL, and panorama role
- Next/previous view and remote navigation buttons
- Screen and LED brightness
- Landscape and soundscape volume
- Widgets, daily routine, and sound-only switches
- Home Assistant Bluetooth adapters and compatible active Bluetooth proxies

## Install for development

1. Copy `custom_components/atmoph_window` into the Home Assistant `custom_components`
   directory.
2. Restart Home Assistant.
3. Close the Atmoph phone app so it does not hold the device's BLE connection.
4. Open **Settings → Devices & services → Add integration → Atmoph Window**.

The advertised name is carried in a scan response, so at least one Bluetooth
scanner must use active scanning. The window may rotate its BLE address; the
integration follows the stable advertised name and re-resolves the current
address before connecting.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest -q
```

Reverse-engineering inputs belong under `.work/` and are intentionally ignored.
Do not commit APKs, decompiled vendor code, packet captures, MAC addresses,
serial numbers, private IP addresses, or authentication material.

## Documentation

- [BLE protocol](docs/PROTOCOL.md)
- [Android app analysis](docs/ANDROID-APP-ANALYSIS.md)

The files under `docs/` are the source of truth. GitHub Actions publishes them
to the repository wiki after changes merge to `main`.

## Related work

- [`glandecki-dev/atmoph-window-yo-ble`](https://github.com/glandecki-dev/atmoph-window-yo-ble)
  independently captured the same protocol on Window Yo and is MIT licensed.
- [`samuel95207/Atmoph-HomeAssistant`](https://github.com/samuel95207/Atmoph-HomeAssistant)
  documents a larger Home Assistant implementation. It did not declare a
  license when this project was started, so its code and assets were not copied.

This project is independent and is not affiliated with or endorsed by Atmoph Inc.
