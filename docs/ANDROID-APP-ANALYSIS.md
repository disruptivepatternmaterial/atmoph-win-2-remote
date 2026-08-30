# Android app analysis

## Acquisition and integrity

The analyzed artifact is Atmoph Window 2 2.3.4 (`com.atmoph.remote`, version
code 2030400), downloaded from Aptoide's package mirror on 2026-08-29.

- SHA-256: `03e9bcbbe3ba3a1e137e48300bcc502028e902a9e07a99e5708d218404561c68`
- MD5 published by the mirror and independently matched:
  `57dacf3172ed84069281e485ed315d79`
- Android manifest distribution stamp: `https://play.google.com/store`
- Signing certificate SHA-256:
  `8F:09:68:6C:33:BA:41:31:A4:16:E0:97:7F:D1:5D:3D:76:0C:3A:41:79:F4:F0:C4:6A:C3:69:E0:76:9B:C4:BE`

The APK itself, extracted files, portable Java runtime, and decompiler output
live under the git-ignored `.work/` directory. They must not be committed or
redistributed.

## Tooling

- Eclipse Temurin JRE 21.0.12.1
- JADX 1.5.6

JADX recovered the complete Atmoph BLE classes despite reporting errors in
third-party and Kotlin metadata passes. Relevant classes:

- `com.atmoph.remote.bluetooth.AtmophBLEUUID`
- `com.atmoph.remote.bluetooth.CasterBleManager`
- `com.atmoph.remote.bluetooth.ControlCommand`
- `com.atmoph.remote.bluetooth.ControlDataType`
- `com.atmoph.remote.bluetooth.QuickMenuSetting`
- `com.atmoph.remote.ui.home.HomeViewModel`
- `com.atmoph.remote.ui.home.HomeFragment`

## What the code says

1. `HomeViewModel` starts an active BLE scan filtered to the Atmoph vendor
   service UUID.
2. `CasterBleManager` resolves ten characteristics from that service.
3. Connection initialization requests a larger MTU, reads identity and view
   metadata, then subscribes to view, quick-setting, focused-element, and
   power notifications.
4. `HomeViewModel` writes the selected `ControlCommand` token as UTF-8 to the
   command characteristic with response.
5. The app's power button selects `ControlCommand.Sleep`, token `S`.
6. Quick settings are represented by the exact JSON field names documented in
   [PROTOCOL.md](PROTOCOL.md).
7. Text input uses a separate JSON characteristic and app-embedded AES key
   material. The key is intentionally not reproduced.

## Network observations

Static extraction found no Atmoph device-control HTTP, WebSocket, or MQTT
endpoint in the app bytecode. Atmoph's support URL and standard Firebase
telemetry endpoints were present. The Android network security configuration
allows cleartext traffic, but that setting alone is not evidence of a LAN API.

This supports implementing the integration through Home Assistant Bluetooth
first. Device-side network discovery remains tracked separately because any
local listener would exist on the window firmware, not necessarily in this
BLE-only controller app.

## Reproducing the analysis

1. Install the official app from Google Play on an Android device.
2. Extract the installed package with ADB or acquire the same signed artifact.
3. Record package version, SHA-256, and signing-certificate fingerprint.
4. Decompile with JADX.
5. Locate the classes above and compare UUIDs, command tokens, initial reads,
   notification subscriptions, and setting serialization.
6. Enable Android Bluetooth HCI snoop logging and capture one UI action at a
   time to verify the static interpretation against real traffic.

The HCI capture is the remaining evidence needed to promote app-verified
commands to hardware-verified commands.
