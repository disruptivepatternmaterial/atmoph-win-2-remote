# Atmoph Window 2 Remote

Local control of the [Atmoph Window 2](https://atmoph.com/en/products/aw102) from
Home Assistant, over Bluetooth Low Energy. No cloud account, no IFTTT, no
polling a vendor API — the integration speaks the same GATT protocol the
official Android app speaks, directly to the window.

[![ci](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/actions/workflows/ci.yml/badge.svg)](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/actions/workflows/ci.yml)
[![validate](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/actions/workflows/validate.yml/badge.svg)](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/actions/workflows/validate.yml)

## Why this exists

The Window 2 is a beautiful piece of hardware with no local API. Control is the
phone app, the physical button, Siri, or the dedicated remote. IFTTT was the
only automation hook, and the newer Window Yo dropped even that.

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

Working on real hardware. The first live run confirmed the parts that had only
ever been inferred from decompiled code: service-UUID discovery by active scan,
connecting with no pairing or authentication, MTU negotiation, the initial read
sequence, and notification subscriptions.

Systematic per-claim validation against an AW102 is still open in
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
| Views can be stepped, not chosen | No characteristic selects a view. The one that looks like it does silently discards writes ([#7](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/7)) |
| A window seen without a name is missed | The name is only in the scan response; the stable ID needs a connection first ([#8](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/8)) |
| No text entry | The app encrypts typed text with a key hardcoded in the APK. Not needed for control, and not reproduced here |
| No zoom | The pinch gesture maps poorly to a Home Assistant entity |
| Not in the HACS default store | Needs a brand assets submission upstream ([#10](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/10)) |

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
