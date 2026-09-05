# Atmoph Window 2 BLE protocol

## Evidence

This specification was recovered from Atmoph Window 2 Android app 2.3.4:

| Field | Value |
|---|---|
| Package | `com.atmoph.remote` |
| Version | `2.3.4` (`2030400`) |
| APK SHA-256 | `03e9bcbbe3ba3a1e137e48300bcc502028e902a9e07a99e5708d218404561c68` |
| APK MD5 | `57dacf3172ed84069281e485ed315d79` |
| Compile / target SDK | 36 / 35 |
| BLE stack | Nordic Semiconductor Android BLE library |

The APK advertises a Google Play distribution stamp and is signed with a
Google app-signing certificate. APKs and decompiled output are not distributed
by this repository.

### How claims below are labelled

| Label | Means |
|---|---|
| **App** | Read directly out of our own decompilation of 2.3.4. First-hand. |
| **Cross-confirmed** | Independently found on Atmoph hardware by [`atmoph-window-yo-ble`](https://github.com/glandecki-dev/atmoph-window-yo-ble) (MIT), from live BLE traffic on a Window Yo. |
| **Reported** | Observed on hardware and published by [`samuel95207/Atmoph-HomeAssistant`](https://github.com/samuel95207/Atmoph-HomeAssistant), which declares no license. Treated as a lead to re-verify, never as code to copy. |

No claim below is marked verified against an Atmoph Window 2 (AW102) by this
project. That is [issue #5](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/5).

Two models share this protocol. The app is branded "Window 2" and drives
Window 2 hardware, but every published hardware observation so far was made
against a Window Yo. Where the two are known to differ, it is called out.

## Discovery and connection

The app actively scans for this 128-bit service (**App**, **Cross-confirmed**):

`c1e0d952-12f7-4c84-b67d-fc26f55243a0`

It uses the GAP name as the visible device name, connects without an
application-level authentication exchange, discovers the service, and requests
**MTU 128** before its initial reads and notification subscriptions (**App** —
`MtuRequest` carries a hardcoded 128, giving a 125-byte ATT payload).

There is no pairing, bonding, or application-layer authentication
(**App**, **Cross-confirmed**).

### Identity is not the address

The BLE address must not be treated as identity. Two independent constraints
apply, and together they rule out both obvious identifiers:

- **The address rotates.** Windows advertise from a resolvable private address.
  Rotation as fast as every ~40 seconds has been reported, which is short
  enough that a single slow connection attempt can outlive the address it
  started from. Re-resolve before each retry rather than reusing the address a
  attempt began with. (**Reported**)
- **The advertised name is not on every packet.** The name rides in the scan
  response, not the advertisement, so a passive scanner sees a nameless device
  and the same window appears named and nameless seconds apart. At least one
  scanner must use active scanning. (**Reported**; consistent with **App**,
  which scans actively.)

The only stable identifier is the device UUID read over GATT from
`5a388825-…` after connecting. It does not rotate, but it cannot be a
discovery key, because rediscovery has only an advertisement to match against
and reading the UUID needs a connection.

The integration therefore splits the two roles. Discovery and the config
entry's own unique id use the advertised name, which is the only value an
advertisement carries when it carries anything. Entities and the device
registry key on the device UUID, adopted the first time a window reports one
and then never replaced — a window answering with a different UUID is a
different window, and following it would orphan the history of the one the
entry was set up for. The switch is a registry migration, so entity ids and
their history survive it. See `custom_components/atmoph_window/identity.py`.

## GATT map

### Primary service `c1e0d952-…`, characteristics the app binds

All ten are **App**-verified; the field letter is the obfuscated
`CasterBleManager` field, kept so the mapping back to decompiled source stays
checkable.

| Characteristic | Field | Direction | Android app purpose | HA use |
|---|---|---|---|---|
| `5a388825-de5b-45bf-8864-16be820fc169` | `o` | Read | Device UUID and name | Stable identity |
| `ec812b51-ae67-4cf3-8272-3967b3fc22a0` | `p` | Read, notify | Panorama role | Sensor attribute |
| `1d862803-b301-4548-bece-1f1ab61881b8` | `q` | Read, notify | Current view title | Sensor state |
| `99cd2547-0640-485c-9996-e0a2b384a6f2` | `r` | Read, notify | Current view image URL | Sensor attribute |
| `275ddae2-4c69-4638-97d4-d5ba8e9e05d1` | `s` | Read, notify | Current view location | Sensor attribute |
| `3afad096-8cb5-4cb7-a8b4-c7dca3e41b94` | `t` | Notify | Focused UI element JSON | Not implemented |
| `d4393824-471f-4799-ab74-28879878a4e7` | `u` | Write | Remote control | Buttons and power |
| `73de7799-b573-46f3-99fd-c6a7a8fc2fde` | `v` | Write | Text input JSON | Not implemented |
| `530bcd10-f723-4203-8222-0e135022d394` | `w` | Write, notify | Quick settings JSON | Numbers and switches |
| `7607f5a4-22bc-4730-9019-c78dc8b50341` | `x` | Read, notify | Display power | Display switch |

`5a388825-…` returns `"<36-char UUID>,<name>"`. The app slices `[0:36]` and
`[37:]`, so the separator at index 36 is a comma and the name may itself
contain commas. The name here is the one set in the app, which is not the
advertised name.

`ec812b51-…` matches only the first character against `Leader` / `Follower` /
`None`, so `L`, `F`, and `N` are sufficient.

### Primary service `c1e0d952-…`, characteristics the app declares but never binds

Present in `AtmophBLEUUID` and confirmed in our own decompilation, but never
assigned to a `CasterBleManager` field, so 2.3.4 never reads or writes them.

| Characteristic | Reported value on hardware | Looks like |
|---|---|---|
| `03cffbfe-b23a-4c8f-bf57-9591b4d59119` | `LAT2_IUOV6NFQ/7206c70d` | Current view id + revision — the tail of the thumbnail URL, and the only characteristic that actually tracks the view. Read best-effort; see below |
| `e6f3269f-a0ce-49fa-9c46-8edbc02e0711` | a bare UUID | Device UUID on its own |
| `2e109d28-1008-4cb6-a7af-1fabb2fa3278` | a device name | Device name on its own |
| `d78f7085-8a3e-487e-8691-9b672aeea0eb` | — | Write-only, purpose unknown |
| `bef2f796-7d49-48e1-9da6-f24346e6aaf7` | — | Constructed in `AtmophBLEUUID` and then discarded; never stored, never used. Possibly the original Atmoph Window service. |

`bef2f796-…` appears in our 2.3.4 decompilation and in no published analysis.

#### The view id is the one of these the integration reads

`1d862803-…` carries a view title, which is what a person reads but a poor key
for an automation: titles repeat across the catalogue and follow the app's
language. `03cffbfe-…` is the catalogue id, so it is the value worth having.

The evidence for it is uneven and the implementation reflects that. That the
UUID exists is **App**. That a window answers a read of it, and that the value
is `<id>/<revision>`, is **Reported** only — 2.3.4 never binds it, so our
decompilation says nothing about its behaviour, and no AW102 has been checked.
The integration therefore treats it as optional at every step: the subscription
may be refused, the read may fail, and the first failure stops it asking again
for the life of the connection. A window that does not implement it gets no
view-id sensor at all rather than one stuck unavailable, and setup, the poll,
and the `current_view` sensor are unaffected. The entity is diagnostic for the
same reason. See `_read_view_id` in `custom_components/atmoph_window/client.py`
and [issue #14](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/14).

The id and the revision are exposed apart because only the id is stable: the
revision moves when Atmoph re-renders a view, which would break the equality
check an automation is trying to make.

The app also builds a synthetic in-process GATT table with per-characteristic
property flags. Because it registers each descriptor under the characteristic's
own UUID — invalid for real GATT — it is a development fixture describing what
the app *expects*, not what a window *exposes*. Its flags are not evidence.
Where they disagree with hardware, hardware wins; see
[Declared properties lie](#declared-properties-lie).

### A second service the app never mentions

Dumping the GATT table off a real window turns up an entire second primary
service. The service UUID itself is absent from the app bytecode, but six of
its characteristics do appear in the decompilation's UUID table, so the app
knows of them while binding none: `e9c45eb5-…`, `ac0c2536-…`, `750b35af-…`,
`492783d7-…`, `822962c8-…`, and `2330f10b-…`.

Every value and behaviour below is **Reported** only, and none of it is
implemented.

Service `401f7f45-2258-4f9b-8204-f8b301b4dcc5`:

Grouped by how much can be said about each:

**Interpretable.** `596f4372-1456-4038-8bca-19ef89e6fe3e` (read, write) carries
a one-key JSON document, `IsLocked`, and is the child lock. It is the only
writable characteristic in this service with an evident purpose, and no attempt
to write it has been recorded. `e9c45eb5-fa81-4760-9b1b-24d6cb1d562c` (read,
write, notify) and `ac0c2536-1713-4be5-97e6-2c281ebb2544` (read, notify) both
carry view identifiers, the latter with a trailing boolean; see
[Views can be stepped, not chosen](#views-can-be-stepped-not-chosen) for why
neither is usable for selection.

**Shape known, purpose not.** `750b35af-a702-4407-95a9-5af779a61785` (read,
write) holds 32 hexadecimal characters, consistent with a token or a digest.
`d39a8ae0-a159-4efd-8ee4-c10c698f5fe2` (read, write, notify) holds a
comma-separated pair whose second field uses the same single-letter encoding as
the panorama role. `e0b4d938-0dec-4e84-ace6-4d81fe4007b6` (read, write) reads
as an empty JSON object.

**Unknown.** `492783d7-…`, `822962c8-…`, `b95dc23d-…` and `18046ba0-…` are
readable, writable and notifying, and empty.
`2330f10b-d28c-4b0e-89c7-8dbd05dfa491` is write-only, so nothing can be learned
from it without writing to it.

A three-field identity variant `b5b8a6c1-79fb-4220-a5b7-90bbc86a732d`
(`uuid`, blank, `name`) is reported on hardware and is absent from our
decompilation entirely.

The window also implements standard Generic Access: `2a00` holds the advertised
name, and `2aa6` Central Address Resolution reads `01`, consistent with the
address privacy above.

## Connection initialization

The app performs this sequence (**App**):

1. Discover the vendor service.
2. Request MTU 128.
3. Read panorama role, identity, view title, image URL, and location.
4. Subscribe to panorama role, view title, image URL, location, focused-view
   JSON, quick-settings JSON, and power.
5. Write command `C` when the remote panel opens so the window announces state.

Home Assistant performs the same operations with three deliberate differences.

It **does not request an MTU**. The app asks for 128 explicitly because the
Android BLE API expects it to; a bleak client cannot portably, since BlueZ and
CoreBluetooth negotiate on the host's behalf. Nothing may therefore assume a
payload size: a notification can be split at any byte, including the middle of
a character. Both readers therefore decode incrementally — the quick-settings
one, and the view title and location, which are the characteristics Atmoph
fills with Japanese. A character arriving in two halves raises on both of them
if each packet is decoded on its own, so the incomplete tail is held until the
rest of it turns up. A long ASCII value split in two is not recoverable and
nothing pretends otherwise: the payload carries no length and no terminator,
so two halves cannot be told apart from two values.

It **subscribes before reading**, reversing steps 3 and 4, so a notification
that fires while the initial reads are in flight is delivered rather than
missed.

It also **reads** the power characteristic, which the app never does. Power
notifies only on change, so nothing arrives at connect and the display state
would otherwise stay unknown until someone toggled the screen. (**Reported**)

See `initialize` in `custom_components/atmoph_window/client.py`.

## Remote-control characteristic

Payloads are bare UTF-8/ASCII tokens written with response. There is no length
prefix or terminator. All tokens are **App**-verified from the
`ControlCommand` enum; `S`, `FW`, `BW`, and `C` are additionally
**Cross-confirmed**.

| Token | App enum | Meaning | HA entity |
|---|---|---|---|
| `T` | Tap | Select / confirm | Select button |
| `DT` | DoubleTap | Double tap | `send_command` service |
| `U` | Up | Navigate up | Up button |
| `D` | Down | Navigate down | Down button |
| `L` | Left | Navigate left | Left button |
| `R` | Right | Navigate right | Right button |
| `FW` | Forward | Next view | Next view button |
| `BW` | Backward | Previous view | Previous view button |
| `M` | Menu | Main menu | Menu button |
| `Q` | QuickMenu | Quick menu | Quick menu button |
| `B` | Back | Back | Back button |
| `V` | Views | Views screen | Views button |
| `VS` | Search | Search | `send_command` service |
| `S` | Sleep | Toggle display sleep | Display switch |
| `C` | ConnectNotify | Request current state | Initialization |
| `SB` | ScaleBegin | Begin pinch gesture | Not implemented |
| `SG` | Scaling | Pinch scale factor | Not implemented |
| `SE` | ScaleEnd | End pinch gesture | Not implemented |
| `P` | Power | Declared and never sent by the app | Not implemented |

### Every implemented token is reachable, but not all as buttons

`DT` and `VS` are the two implemented tokens with no entity of their own. A
double tap and a jump to the search screen are one-shot inputs whose result
depends on what the window is already showing, and search leads to a text field
this integration deliberately cannot fill, so neither earns a button on a
dashboard. Both are sent by the `atmoph_window.send_command` service, which
validates its argument and refuses anything it does not recognise. The
argument is the name the integration uses, not the token or the app's enum:
`double_tap` and `search`, never `DT`, `VS`, or `DoubleTap`. `services.yaml`
offers the full list in the picker. That service also covers `S` and `C`, which entities own for other reasons:
`S` belongs to the display switch because only the switch performs the
read-and-confirm dance the toggle needs, and `C` runs at connect.

`P`, `SB`, `SG`, and `SE` are not implemented at all, so the service does not
offer them either.

### Zoom is the one non-token payload

`SB` and `SE` are bare tokens, but `Scaling` is written to the same
characteristic as a JSON object carrying the current scale factor (**App**):

```json
{"SG": 1.25}
```

The sequence is `SB` → `{"SG": …}` × n → `SE`. Any parser for this
characteristic must therefore tolerate both bare tokens and JSON.

### Power is a toggle, and only a toggle

The app declares `P` (`Power`) but its power button sends `S`, which toggles
(**App**). Three separate findings mean an absolute on/off is not available:

- Writing `true` / `false` to `7607f5a4-…` is silently discarded in both
  directions, despite the characteristic advertising `write`. (**Reported**)
- `S` sent within roughly a second of a previous `S` that had already taken
  effect is dropped, with no ATT error and no state change. (**Reported**)
- Power notifies only on change, so a dropped write is indistinguishable from a
  slow one without reading. (**Reported**)

The integration therefore reads `7607f5a4-…`, sends `S` only if the state
differs, polls for confirmation, and retries once after a longer pause before
giving up. See `set_power` in `custom_components/atmoph_window/client.py`.

### Declared properties lie

Two characteristics advertise `write` and discard writes: `7607f5a4-…` (power,
above) and `e9c45eb5-…` (view id, below). Treat this device's declared GATT
properties as a weak hint and confirm behaviour against reported state.

### Views can be stepped, not chosen

No characteristic selects a view. `FW` and `BW` advance and retreat through the
catalogue one entry at a time, and that is the entire navigation surface this
protocol offers.

Two characteristics hold something resembling a view identifier, and it is
worth being precise about which does what, because the naming invites the wrong
conclusion:

- `03cffbfe-…`, in the main service, is the one that actually follows the
  display. Its value is `<id>/<revision>` — the same pair the thumbnail URL
  path is built from. It is read and notify only, so it reports and cannot be
  commanded.
- `e9c45eb5-…`, in the second service, declares write as well, and its value
  has the shape of an identifier. Neither fact means what it appears to.
  Reported testing found its value frozen on one identifier while the display
  moved through several, and found writes to it — whether an identifier alone,
  an identifier with a revision, or the identifier already on screen —
  acknowledged at the transport level while changing neither the display nor
  the characteristic's own value.

So a view-selection entity is not implementable today. It would require either
a command nobody has found or a purpose for one of the two write-only
characteristics that remain unidentified, `2330f10b-…` and `d78f7085-…`.
Tracked as
[issue #7](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/7).

Everything in this section beyond the two UUIDs and their declared properties
is **Reported** and has not been reproduced here.

## Quick settings

The app writes a compact single-key JSON object to `530bcd10-…` (**App**):

```json
{"ScreenBrightness":6}
```

Notifications contain the complete settings object. Booleans are JSON
booleans; levels are objects with device-provided bounds:

```json
{"ScreenBrightness":{"min":1,"max":10,"value":6}}
```

Known keys (**App**, from `QuickMenuSetting`), with how each is exposed:

| Key | Shape | HA surface |
|---|---|---|
| `WidgetsVisible` | boolean | Switch |
| `DailyRoutineEnable` | boolean | Switch |
| `SoundOnly` | boolean | Switch |
| `LandscapeVolumeLevel` | level | Number |
| `SoundscapeVolumeLevel` | level | Number |
| `ScreenBrightness` | level | Number |
| `LedBrightness` | level | Number |
| `SoundscapeLayer` | level | `set_setting` service |
| `CurrentDecoration` | level | `set_setting` service |

`SoundscapeLayer` and `CurrentDecoration` are levels by wire format but choices
by meaning: neither is a magnitude a slider communicates honestly, and nothing
recovered from the app names their members, so a `select` would have to invent
labels. Both stay writable through `atmoph_window.set_setting`, which validates
the key against the table above and a level against the bounds the window
itself reported, refusing to write a setting the window has never reported at
all — there is nothing in the write format to say whether such a key is a
boolean or a level. See
[issue #15](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/15).

### Bounds are per-device and wider than they look

Never hardcode a 0–10 range. One reported device:

| Setting | min | max |
|---|---|---|
| `ScreenBrightness` | 1 | 25 |
| `LandscapeVolumeLevel` | 0 | 24 |
| `SoundscapeVolumeLevel` | 0 | 20 |
| `LedBrightness` | 0 | 20 |
| `CurrentDecoration` | 0 | 19 |
| `SoundscapeLayer` | 0 | 5 |

Read the bounds from the device and use them. (**Reported**)

### Reassembly

JSON may span multiple ATT notifications. The app accumulates chunks and
retries a parse after each one, discarding the buffer after 20 consecutive
failures (`TimeoutJsonMerger`); the focused-view characteristic uses Nordic's
brace-counting `JsonMerger` (**App**). Any implementation should reassemble by
"accumulate until it parses". The integration buffers partial objects, accepts
concatenated complete objects, and enforces a size limit.

A setting write is echoed back on the notify channel, reportedly within about
1.5 s. That echo is the most reliable way to test the write path, because
`DailyRoutineEnable` makes the window rotate views on its own — so an observed
view change is not proof that an `FW` / `BW` write landed. (**Reported**)

## Text input

The app writes JSON shaped as (**App**):

```json
{"type":"text","extra":{"text":"<encoded value>","input_mode":"search"}}
```

and ends input with:

```json
{"type":"text_done","extra":{}}
```

`input_mode` is one of `password`, `credit_card_number`, `credit_card_month`,
`credit_card_year`, `credit_card_cvc`, `text`, `search`, `pin`
(`InputMode.java`).

The value is AES-encrypted, then Base64-encoded, using a key hardcoded in the
app (`TextCipher.java`). Its purpose is to keep typed passwords off the air in
cleartext, not to authenticate anything. The key is recoverable from the APK by
anyone repeating the decompilation, and is deliberately **not** reproduced in
this repository. Text entry is not required for the Home Assistant control
surface and is not implemented.

## Hardware validation checklist

Nothing here has been checked against an AW102 by this project. Doing so is
[issue #5](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/5).

- Confirm the service is advertised by an AW102 running current firmware.
- Dump the real GATT table, both services, and compare properties with this map.
- Confirm whether Window 2 exposes `401f7f45-…` at all, or whether it is
  Window Yo only.
- Read identity, view metadata, quick settings, and power; record real bounds.
- Read `03cffbfe-…` and record whether an AW102 answers it at all, whether it
  notifies, and whether the value really is `<id>/<revision>`. The view-id
  sensor rests entirely on this and nothing else in the integration does
  ([issue #14](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/14)).
- Send `C` and confirm notifications arrive.
- Test every implemented command once, with the official app closed.
- Verify `S` in both directions, then verify the dropped-toggle window by
  sending two in quick succession.
- Confirm power writes are discarded, so the toggle path stays justified.
- Capture an HCI snoop log and promote **App** claims to hardware-verified.
- Observe address rotation and reconnect through a Home Assistant Bluetooth
  proxy.

## Reproducing this analysis

See [ANDROID-APP-ANALYSIS.md](ANDROID-APP-ANALYSIS.md) for acquisition,
tooling, and the class-by-class walkthrough.

Whether the window exposes any network control surface was settled in
[issue #6](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/6),
now closed: a full 1–65535 TCP scan of a real unit found one open port,
`53/tcp`, silent to both a banner grab and an HTTP request, and every Android
diagnostic port closed. The app is BLE-only and so is the window, so this
integration is too.
