# Atmoph Window 2 Remote

Local Bluetooth Low Energy control for Atmoph Window 2 through Home Assistant.
The integration reproduces the official Android app's GATT operations without
requiring Atmoph cloud access.

## Status

The Android app protocol has been statically verified against Atmoph Window 2
app 2.3.4 (`com.atmoph.remote`, version code 2030400), and the offline protocol
tests pass against a simulated peripheral.

Nothing here has been confirmed against a real Window 2 yet. `v0.1.0` is
published so it can be installed and exercised on hardware, which is the only
way that confirmation happens — expect to find things. Progress is tracked in
[issue #5](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/5).

## Supported controls

- Automatic Bluetooth discovery and UI setup
- Display wake/sleep with read-before-toggle state confirmation
- Current view title, location, image URL, and panorama role
- Next/previous view and remote navigation buttons
- Screen and LED brightness
- Landscape and soundscape volume
- Widgets, daily routine, and sound-only switches
- Home Assistant Bluetooth adapters and compatible active Bluetooth proxies

## Install

### HACS

Add this repository as a custom repository with category **Integration**, then
download it. `hacs.json` sets `zip_release`, so HACS installs the
`atmoph_window.zip` asset attached to a release and will not install from the
default branch — if a download fails, check that the release you are pointed at
actually carries that asset.

### Manually

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

The protocol suite runs without Home Assistant, which is what keeps
`protocol.py` and `client.py` reusable outside it:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest -q
```

The Home Assistant layer has its own suite. It needs the real Home Assistant,
so it runs on its own and is excluded from the command above:

```sh
.venv/bin/pip install -e '.[homeassistant-test]'
.venv/bin/pytest -q tests/homeassistant
```

`pytest-homeassistant-custom-component` pins one exact Home Assistant version
per release, so bumping the Home Assistant this is tested against means bumping
that pin. `homeassistant.components.bluetooth` also depends on the `usb`
integration, whose requirements Home Assistant only installs when it sets that
component up — `aiousbwatcher` and `serialx` are therefore listed explicitly.

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
