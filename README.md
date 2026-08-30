# Atmoph Window 2 Remote

Local control of the [Atmoph Window 2](https://atmoph.com/en/products/aw102) from
Home Assistant, over Bluetooth Low Energy. No cloud account, no IFTTT, no
polling a vendor API — the integration speaks the same GATT protocol the
official Android app speaks, directly to the window.

[![ci](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/actions/workflows/ci.yml/badge.svg)](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/actions/workflows/ci.yml)
[![validate](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/actions/workflows/validate.yml/badge.svg)](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/actions/workflows/validate.yml)

## Why this exists

The Window 2 is a frustrating piece of hardware with no local API. Control is
the phone app, the physical button, Siri, or the dedicated remote. IFTTT was
the only automation hook, and the newer Window Yo dropped even that. Support
has been abysmal, and many of mine have broken — expensive trips from the USA
to Japan. While I love mine, I would not "invest" in them again.

Everything the app can do, it does over an unauthenticated BLE service. So it
can be done from Home Assistant instead, on your own network, with no
dependency on Atmoph's servers staying up or their app staying installed.

## What you get

One device per window, with:

| Platform | Entities |
|---|---|
| Switch | Display power, widgets visible, daily routine, sound only |
| Number | Screen brightness, LED brightness, landscape volume, soundscape volume |
| Button | Next and previous view, menu, quick menu, views, back, tap, and a four-way d-pad |
| Sensor | Current view title, with location, thumbnail URL, and panorama role as attributes |

Display power is idempotent. The protocol only offers a *toggle*, so
`switch.turn_off` reads the current state first, sends the toggle only if it
needs to, then confirms — meaning you can safely call it from an automation
that fires repeatedly.

Brightness and volume ranges are read from the window rather than assumed. The
bounds are per-device and wider than you would guess.

## Status

The app-derived protocol and Home Assistant behavior are covered by offline
tests, but this project has not yet verified them against an AW102. Hardware
validation is open in
[issue #5](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/5),
and `docs/PROTOCOL.md` labels every claim by how strong the evidence for it
actually is. Treat anything not marked hardware-verified as a good hypothesis.

## Install

### HACS

Add this repository as a custom repository with category **Integration**, then
download it.

`hacs.json` sets `zip_release`, so HACS installs the `atmoph_window.zip` asset
attached to a release and **will not** install from the default branch. If a
download fails, check that the release you are pointed at actually carries that
asset.

### Manually

1. Copy `custom_components/atmoph_window` into your Home Assistant
   `custom_components` directory.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Atmoph Window**.

### Before it will find anything

- **Close the Atmoph phone app.** The window accepts one central connection at
  a time and the app holds it.
- **At least one Bluetooth scanner must use active scanning.** The advertised
  name rides in the scan response, not the advertisement, so a passive scanner
  sees a nameless device. Home Assistant's own adapters and active
  [Bluetooth proxies](https://esphome.io/components/bluetooth_proxy.html) both
  work.

Windows advertise from a rotating private address, so the integration keys on
the advertised name and re-resolves the current address before each connection
attempt.

## Known limitations

| Limitation | Why |
|---|---|
| Views can be stepped, not chosen | No characteristic selects a view. The one that looks like it does silently discards writes ([#7](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/7)). The [Node-RED example](#example-node-red-control) skips unwanted views by pressing next |
| A window seen without a name is missed | The name is only in the scan response; the stable ID needs a connection first ([#8](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/8)) |
| No text entry | The app encrypts typed text with a key hardcoded in the APK. Not needed for control, and not reproduced here |
| No zoom | The pinch gesture maps poorly to a Home Assistant entity |
| The device page shows a generic puzzle piece | Home Assistant loads icons from `brands.home-assistant.io`, never from the integration folder, so it needs an upstream submission ([#10](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/10)) |

## Example: Node-RED control

[`examples/nodered-atmoph-windows.json`](examples/nodered-atmoph-windows.json)
is a full Node-RED tab exported from a live three-window panorama on 29 Aug
2026. Import it, point the Home Assistant nodes at your server, and rewrite
the `office_left_*` entity ids.

It uses the entities this integration already exposes — display, daily
routine, widgets, next view, and current view with `location` / `image_url`
attributes — plus two helpers (`input_boolean.good_morning` /
`input_boolean.good_night`) that are not part of this project.

| Piece | What it does |
|---|---|
| Good morning / good night | Turns display, daily routine, and widgets on in the morning; display off at night |
| Time inject | Cron `*/10 * * * *`: if the display is on, press next on the left window |
| Current view changed | Wait 10 s, re-read the sensor, geocode `location` via Nominatim (cached), skip if the country / view / location is on the blocklist |
| BLOCKLIST - edit me | Flow-context JSON: `always` (cannot be skipped), `countries`, `views`, `locations` |
| last 100 views | Newest-first ring buffer in flow context `views`, which is how a "too city like" or "beachy" view gets promoted into the blocklist |

Pressing next on the left window moved all three in that install (right about
5 s later, middle about 30 s). They are one panorama, so a skip here skips it
everywhere. Location lagged the view title by about 4 ms; the 10 s settle is
the delay node, not a measurement of how long the window needs.

Un-geocoded places are shown, never skipped. Always-allow wins, which is why
Japan cannot be blocked even if a lagging location attribute momentarily
points at a blocked country. Five blocked views in a row stop pressing so a
long run cannot hammer the window.

Palette: `node-red-contrib-home-assistant-websocket` and
`node-red-contrib-sun-position`. The link-out to an Error Sink tab is
optional and will be dangling unless you have that tab. Change the Nominatim
`User-Agent` if you are not the original exporter. Edit the blocklist node
and deploy; a boot inject reloads it on Node-RED start.

## How the protocol was recovered

Static analysis of Atmoph Window 2 app 2.3.4 (`com.atmoph.remote`, version code
2030400), decompiled with JADX, then cross-checked against an independent
capture of the same protocol on different hardware.

The interesting findings are the ones that contradict what the app appears to
say:

- **Declared GATT properties lie.** Two characteristics advertise `write` and
  discard writes — including the power characteristic, which is why display
  control has to go through the toggle.
- **A toggle sent too soon is dropped.** Around a second after a previous one
  that landed, the window ignores it, with no ATT error. Automations that
  assume a write took effect will drift.
- **Power notifies only on change.** Nothing arrives at connect, so the state
  is unknown until someone touches the screen. The app never reads it; this
  integration does.
- **There is a second service the app never mentions**, holding a writable
  child lock among other things.
- **`Scaling` is not a bare token.** Unlike every other command it writes JSON
  to the command characteristic, so a parser has to handle both.

Full detail, with an evidence tier on every claim, is in
[docs/PROTOCOL.md](docs/PROTOCOL.md). Acquisition, tooling, and the
class-by-class walkthrough are in
[docs/ANDROID-APP-ANALYSIS.md](docs/ANDROID-APP-ANALYSIS.md).

## Diagnosing a misbehaving window

`tools/atmoph_diag.py` dumps a window's entire GATT table — both services,
every declared property, every readable value — and turns the quick-settings
document into a verdict. Two units can be dumped with `--normalize` and
compared with `diff`, which is how a firmware or model difference gets told
apart from a hardware fault without opening anything or shipping anything.

`tools/atmoph_netscan.py android` looks for a diagnostic port and completes a
real ADB handshake against anything that answers, which distinguishes `adbd`
from an unrelated listener.

[docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md) has the procedure and the decision
trees.

## Documentation

- [BLE protocol](docs/PROTOCOL.md) — the recovered protocol, with evidence tiers
- [Android app analysis](docs/ANDROID-APP-ANALYSIS.md) — how it was recovered
- [Diagnostics](docs/DIAGNOSTICS.md) — investigating a window that misbehaves
- [Brand assets](docs/BRAND-ASSETS.md) — icon requirements and submission

`docs/` is the source of truth and is published to the repository wiki
automatically after changes merge to `main`. Edit the sources, never the wiki.

## Development

The protocol suite runs without Home Assistant at all, which is what keeps
`protocol.py` and `client.py` reusable outside it:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest -q
```

The Home Assistant layer has its own suite, which does need the real Home
Assistant and so runs separately:

```sh
.venv/bin/pip install -e '.[homeassistant-test]'
.venv/bin/pytest -q tests/homeassistant
```

`pytest-homeassistant-custom-component` pins one exact Home Assistant version
per release, so bumping the version tested against means bumping that pin.
`homeassistant.components.bluetooth` also pulls in the `usb` integration, whose
requirements Home Assistant only installs when it sets that component up, so
`aiousbwatcher` and `serialx` are listed explicitly.

Reverse-engineering inputs belong under `.work/` and are git-ignored. Do not
commit APKs, decompiled vendor code, packet captures, MAC addresses, serial
numbers, private IP addresses, or authentication material — the repository is
public and `docs/` is published to the wiki automatically.

## Related work and credit

- [`glandecki-dev/atmoph-window-yo-ble`](https://github.com/glandecki-dev/atmoph-window-yo-ble)
  (MIT) independently identified the same service, command, power, and settings
  characteristics from live BLE traffic on a Window Yo. Two people arriving at
  the same map by different routes — decompilation here, packet capture there —
  is the strongest evidence either project has, and its
  [write-up](https://github.com/glandecki-dev/atmoph-window-yo-ble/blob/main/docs/communication-analysis.md)
  is a genuinely good tutorial on the method.
- [`samuel95207/Atmoph-HomeAssistant`](https://github.com/samuel95207/Atmoph-HomeAssistant)
  is a larger Home Assistant implementation with valuable hardware
  observations. It **declares no license**, so no code, text, or assets were
  copied from it. Protocol facts were re-derived from our own decompilation and
  are attributed where a hardware observation originated there.

## Licence

[MIT](LICENSE). Independent project, not affiliated with or endorsed by
Atmoph Inc.
