# Phantom 1.0.0

Phantom is a macOS utility for iOS Core Location development and QA on a USB-connected iPhone.

## Requirements

- Apple Silicon Mac with macOS 11 or later
- iPhone with iOS 17.4 or later
- Developer Mode enabled
- USB data cable and trusted pairing

## Install

1. Download `Phantom-1.0.0-arm64.dmg`.
2. Open the DMG and copy `Phantom.app` to Applications.
3. This community build is ad-hoc signed but not Apple-notarized. Control-click Phantom in Applications and choose **Open** the first time. Do not disable Gatekeeper globally.
4. Connect and unlock the iPhone, then approve Phantom's one-time administrator prompt to install its protected USB tunnel helper.

## Integrity

Verify the SHA-256 against `Phantom-1.0.0-arm64.dmg.sha256` before opening.

## Privacy and limitations

Phantom has no first-party analytics or account system. OpenFreeMap, Nominatim, and OSRM process map, search, and route requests. Phantom changes app-visible Core Location only; it does not alter carrier, emergency-service, IP, physical-camera, or phone-number records.

Use only on devices you own or are authorized to test.
