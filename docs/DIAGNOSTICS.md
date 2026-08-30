# Diagnosing a misbehaving Window 2

This is the procedure for gathering evidence about a window that is doing
something wrong, using only what a window exposes from the outside. Nobody has
analysed the Window 2 firmware, and nothing here requires it.

Two symptoms motivated these tools:

- A unit that will not join a 5 GHz Wi-Fi network.
- A unit whose LEDs will not turn on.

Both are answerable without firmware work, and the answers come from different
places: the LED question from the BLE quick-settings document, the Wi-Fi
question from the access point configuration and, if the window will allow it,
from Android's own logs.

## How claims here are labelled

| Label | Means |
|---|---|
| **Verified** | Checked by this project, and how is stated. |
| **Reported** | Observed on Atmoph hardware and published elsewhere; see [PROTOCOL.md](PROTOCOL.md) for sources. |
| **Inference** | Follows from documented Android or Wi-Fi behaviour, not from an Atmoph observation. Treat as a lead. |

Nothing in this document has been confirmed against an Atmoph Window 2 by this
project. The tools' own logic has been; see
[Verification status](#verification-status-of-the-tools-themselves).

## Before you start

- **Close the Atmoph phone app.** It holds the BLE connection, and a second
  central cannot connect while it does.
- **Do not run either tool from a sandboxed shell.** macOS refuses Bluetooth to
  a sandboxed process and drops LAN TCP connects, and the failure looks
  identical to "there is nothing there". Both tools now say so rather than
  reporting a false negative, but the only fix is a normal terminal with the
  Bluetooth permission granted.
- **Dump a working unit too.** Almost every conclusion below is a comparison. A
  single dump of a broken window is much weaker evidence than a diff against a
  healthy one.

## The BLE dump

`tools/atmoph_diag.py` connects to a window and records the complete GATT
table, every readable value, and the quick-settings document. It needs only
`bleak`.

```sh
uv run --no-project --with bleak -- python tools/atmoph_diag.py scan
```

or, with `bleak` already installed:

```sh
python3 tools/atmoph_diag.py scan
```

`scan` is the cheap first question: can this host see the window at all? It
scans actively, because the advertised name rides in the scan response rather
than the advertisement, so a window is sometimes nameless and the same unit
appears named and nameless seconds apart (**Reported**). Detections are merged
per address, so a name seen once is kept.

A window's BLE address is a resolvable private address and rotates, reportedly
as fast as every 40 seconds (**Reported**). Never write one down as identity —
the device UUID read over GATT is the only stable identifier.

### Dumping one window

```sh
python3 tools/atmoph_diag.py dump
```

With no address, it scans, then dumps every window that advertised the vendor
service. The report opens with the identity and a summary, then the
quick-settings table, the LED finding, and the full GATT table with each
characteristic's declared properties, value, and descriptors.

The GATT table is worth capturing for its own sake. The Android app binds ten
characteristics in one service; hardware reportedly exposes roughly twice that,
plus an entire second primary service the app never mentions (**Reported**).
Confirming what a Window 2 actually exposes is part of
[issue #5](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/5),
and this dump is how that gets done.

If the quick-settings characteristic is not readable on your firmware, the
report says the LED question is unanswered and asks for `--provoke`, which
sends the app's `C` state-request token so the window announces its settings on
the notify channel. That token requests state and changes none, but it is still
a write, so it is opt-in. Nothing else is written without `--led-write` or
`--write`.

### Comparing a broken unit against a working one

```sh
python3 tools/atmoph_diag.py --normalize dump --max 2 --out dumps
diff dumps/*.txt
```

`--normalize` masks everything that necessarily differs between two units or
two runs: the rotating address, signal strength, timestamps, the negotiated
MTU, and the per-unit identity values. Services and characteristics sort by
UUID rather than by handle, because handles are not stable across firmware
revisions. What survives a diff is structure and settings, which is what you
want to compare. Files are still named after the real device UUID so two
normalized dumps do not collide.

Because `--normalize` removes the device identifiers, its output is also the
form to paste into an issue. Unnormalized output is not: it contains the BLE
address, the device UUID, and the device name. The report says so in its own
header.

Sequential dumps mean the second window's address may rotate between the scan
and the connect. If a connect fails, scan again rather than reusing the
address.

## The LED decision tree

The quick-settings document is written and notified as JSON on `530bcd10-…`.
Levels arrive as objects carrying device-provided bounds (**App**, confirmed in
this project's decompilation):

```json
{"LedBrightness":{"min":0,"max":20,"value":7}}
```

The tool reports one of six verdicts. Each means something different, and only
one of them is worth writing to.

| Verdict | What the window reported | What it means | Do next |
|---|---|---|---|
| `no-document` | Nothing parsed | The question is unanswered, not answered negatively | Re-run with `--provoke`; if still nothing, the notify channel or the connection is the problem, not the LEDs |
| `key-absent` | A populated document with no `LedBrightness` | This firmware does not model LEDs on this unit at all. No write will ever help | Dump a working unit. If it reports the key, the difference is firmware or model configuration, and it is a warranty conversation |
| `zero-max` | `LedBrightness` present, `max: 0` | The firmware knows the setting and believes there is no usable range — it does not think the unit has LED hardware | Same as above: compare bounds against a working unit |
| `malformed` | Present but not a `min`/`max`/`value` object | Cannot be interpreted | Capture the raw document and compare |
| `range-off` | Normal range, `value: 0` | The LEDs are simply switched off | `--led-write` a non-zero value and watch the echo |
| `range-on` | Normal range, `value` above zero | The firmware believes the LEDs are lit | If they are physically dark, the fault is below the settings layer: LED hardware, its cable, or a gate this protocol does not expose |

The tool also reports two settings that could plausibly hold the LEDs off, and
both are worth clearing before drawing any conclusion:

- **`SoundOnly`** — audio-only mode. If true, the panel and plausibly the LEDs
  are dark by design (**Inference**: that it darkens the LEDs specifically has
  not been observed).
- **`CurrentDecoration`** — the decoration frame, reported as a level with real
  bounds. Whether a decoration can gate the LEDs is **unverified**. If the LED
  range looks normal and the LEDs stay dark, step it.

### Testing the write path

A setting write is echoed back on the notify channel, reportedly within about
1.5 seconds (**Reported**). That echo is the only reliable proof that a write
landed, because `DailyRoutineEnable` makes the window rotate views on its own —
so an observed view change proves nothing about whether your write arrived.

```sh
python3 tools/atmoph_diag.py dump --led-write 5
```

The tool records the previous value, writes, waits for the echo, and then
writes the old value back and waits for that echo too. Four outcomes:

| Outcome | Means |
|---|---|
| `echo-matched` | The write path works and the firmware accepted the value. If the LEDs stay dark, the fault is downstream of the setting |
| `echo-clamped` | The firmware accepted the write and changed the value — typically back to zero. Consistent with a unit whose firmware believes it has no LEDs |
| `no-echo` | Nothing came back. Either the write did not land or the notify channel is not delivering. Distinguish the two with a control write to a setting you can see, for example `--write ScreenBrightness=6` |
| `write-rejected` | The ATT write itself failed, and the error is recorded |

The control write is what makes `no-echo` interpretable. If
`--write ScreenBrightness=6` echoes and `--led-write 5` does not, the write
path is fine and `LedBrightness` specifically is being ignored.

## Android diagnostic ports

The Window 2 runs Android on an ARM Cortex-A53. If it leaves the Android Debug
Bridge listening on TCP 5555, that single fact answers both symptoms: `logcat`
shows the Wi-Fi association attempt failing and says why, and `dumpsys wifi`
prints the driver's own view of the country code and the channel list it will
accept.

Whether a Window 2 exposes any diagnostic port is unknown and is
[issue #6](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/6).
There is no LAN *control* API — that question is closed and the answer is no.

First find the window. It is very likely not on the subnet your workstation is
on; get the current lease from the DHCP server rather than guessing.

```sh
python3 tools/atmoph_netscan.py local                  # which /24 am I on?
python3 tools/atmoph_netscan.py sweep <window-cidr>     # find live hosts
python3 tools/atmoph_netscan.py android <window-ip>     # the diagnostic ports
```

`android` probes 5555 (adbd), 5037, 2323 (Fully Kiosk remote administration),
8080, 80, 443, and 5900, and then sends a real ADB connect handshake to every
port that answered. The handshake distinguishes adbd from anything else that
happens to listen there, and distinguishes a device that will accept a
connection from one that will first demand authorisation. Nothing is executed
on the device: the exchange stops at the banner adbd sends unasked.

| Result | Means | Do next |
|---|---|---|
| `open/adb-open` | adbd answered and returned its device banner, which usually includes the product name | `adb connect <window-ip>:5555` |
| `open/adb-auth-required` | adbd is there but wants this host's key authorised | `adb connect <window-ip>:5555`, then watch the window's screen for the prompt |
| `open` with no ADB verdict | Something else is listening | `fingerprint <window-ip> <port>` |
| `closed` | Nothing is listening, and the host answered to say so | Nothing to do |
| `filtered` on every port | **Not** a finding. A timeout is what a sandbox, a firewall, and an absent host all look like | Re-run from an unsandboxed terminal on the window's own subnet |

That last row matters. A sandboxed macOS process gets timeouts for every LAN
connect, so "nothing is open" must never be recorded from inside one. The tool
prints this warning itself when every port times out.

### If ADB is open

The two commands worth running first:

```sh
adb -s <window-ip>:5555 shell dumpsys wifi
adb -s <window-ip>:5555 logcat -d
```

`dumpsys wifi` reports the country code and the supported channel list, which
turns the whole 5 GHz section below from a checklist into a single lookup.
`logcat` taken while the window attempts to join the network shows the
association or authentication failure directly. Nothing else needs to be
installed on the window, and nothing needs to be modified.

## 5 GHz: what to rule out before blaming the window

Every item here is an access point setting, not a window fault. Work down the
list; the first two are the most likely.

### Channel and regulatory domain

A Wi-Fi client only associates on channels its regulatory domain permits. This
is the strongest lead available without any device access, because Atmoph is a
Japanese company and Japan's 5 GHz allocation is unusually narrow.

Japan permits 5150–5250 MHz (W52, channels 36–48), 5250–5330 MHz (W53,
channels 52–64, DFS required) and 5470–5710 MHz (W56, channels 100–140, DFS
required). It does **not** allocate 5735–5835 MHz for wireless LAN, so channels
149–165 are unavailable. This is from the Linux wireless regulatory database's
`country JP` entry, which is the table Android's own Wi-Fi driver derives its
channel list from (**Verified** against `wireless-regdb`; **Inference** that a
given Window 2 ships a JP regulatory domain).

The consequence is concrete. Channels 149–165 are the most commonly used
non-DFS 5 GHz channels outside Japan, and a JP-domain client cannot use any of
them. Two checks, in order:

1. **Move the access point to channel 36, 40, 44, or 48** and retry. Those are
   permitted in every region and need no radar detection. If the window joins,
   the diagnosis is finished.
2. **If it was already on a DFS channel** (52–144), the access point must
   complete its radar-detection dwell before it transmits, which can take a
   minute or more after a reboot or a channel change. A client scanning during
   that window sees nothing at all.

Note that channel 144 sits above Japan's 5710 MHz ceiling even though it is
inside the 100–144 range other regions allow.

### Security mode

WPA2/WPA3 transition mode advertises both an RSN element for WPA2-PSK and one
for WPA3-SAE in the same beacon. Clients with an older `wpa_supplicant` — which
an appliance running a frozen Android build is likely to have — can fail to
associate to it while joining a plain WPA2-PSK network without complaint
(**Inference**).

Set the 5 GHz SSID to **WPA2-PSK (AES/CCMP) only**, with 802.11w management
frame protection **optional or disabled**, and retry. WPA3-only and
PMF-required are both plausible hard stops for an older client.

### Channel width

160 MHz channel width requires VHT160 support the client may not have. A
correct client negotiates down; a cheap chipset with a frozen driver may fail
to associate instead (**Inference**). Set the width to 80 MHz, or 40 MHz, and
retry.

### One SSID on both bands

If 2.4 GHz and 5 GHz share an SSID, band steering decides which radio a client
lands on, and the client has little say. A window that "will not join 5 GHz"
may be joining the SSID perfectly well and being steered to 2.4 GHz every time.

Create a temporary 5 GHz-only SSID with a distinct name, on channel 36, WPA2-PSK
only, 80 MHz, broadcast. If the window joins that, nothing is wrong with the
window, and the original problem is steering or the channel.

### Hidden SSID

A hidden SSID is not discoverable by scanning, so it cannot be selected from a
list — it has to be typed, and the client has to mark it as hidden so it probes
for it by name. A setup flow that only offers a scan list cannot join one at
all. Broadcast the SSID while testing.

### The rest of the list

- **Client isolation or a guest VLAN** on the 5 GHz SSID: the window may
  associate and then be unable to reach DHCP.
- **A MAC allow-list** on the access point, which the window's rotating BLE
  address has nothing to do with — the Wi-Fi interface has its own hardware
  address, and a randomised Wi-Fi address will defeat an allow-list.
- **802.11ax-only or 802.11ac-only** mode, which excludes an 802.11n client.
- **A full DHCP pool**, which looks exactly like a Wi-Fi failure from the
  window's side.

## What the tools cannot tell you

- **Whether the LED hardware works.** A `range-on` verdict with dark LEDs
  narrows the fault to hardware or to a gate below the settings layer, but
  cannot distinguish those two.
- **Why Wi-Fi failed**, unless ADB is open. Without it, the checklist above is
  elimination, not diagnosis.
- **Anything about firmware internals.** No firmware has been analysed, and
  neither tool attempts to.

## Sharing a dump safely

An unnormalized dump contains the BLE address, the device UUID, and the device
name. This repository is public and so are its issues and its wiki. Use
`--normalize`, read the file before attaching it, and keep addresses,
hardware addresses, and serial numbers out of issue text.

## Verification status of the tools themselves

`tools/selftest.py` verifies both tools offline, against a fake window and a
fake adbd rather than against hardware:

```sh
uv run --no-project --with bleak -- python tools/selftest.py
```

It covers value rendering, the quick-settings description, all six LED
verdicts, both gate settings, every write-echo outcome, JSON reassembly across
notifications, report normalization, filename derivation, MTU reporting, and
the ADB handshake against a real socket serving synthetic CNXN, AUTH, non-ADB,
and silent replies.

What it does not cover, because it cannot without hardware: the BLE scan, the
connect, and the real GATT enumeration. Those paths are written and unrun.
