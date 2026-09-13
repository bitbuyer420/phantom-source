# Phantom Privacy Notice

**Effective:** September 12, 2026

Phantom is a local macOS utility for testing iOS Core Location on a USB-connected iPhone.

## Data Phantom does not collect

Phantom has no user accounts, analytics, advertising SDK, crash-reporting service, or developer-operated telemetry endpoint. It does not upload your Apple Account, phone number, contacts, messages, IMEI, or iPhone UDID to a Phantom service.

Device details needed by the interface—device name, model, iOS version, and UDID—are read locally over USB and remain on the Mac.

Bookmarks, recent locations, and the last simulated coordinate are stored locally at `~/.config/phantom/config.json`.

## Third-party network services

The current release uses third-party public mapping services:

- **OpenFreeMap** for map tiles. The service can observe the requesting IP address and requested map tile areas.
- **OpenStreetMap Nominatim** for place search. The service receives search text and the requesting IP address.
- **OSRM demo routing** for road routes. The service receives route waypoints and the requesting IP address.

Do not enter sensitive or confidential locations if you do not want those providers to process them. Their own privacy and retention policies apply.

## Local privileged helper

With administrator approval, Phantom copies its USB tunnel runtime to `/Library/PrivilegedHelperTools/com.ctwebsolutions.phantom` and creates `/Library/LaunchDaemons/com.phantom.tunneld.plist`. The helper listens only on the Mac's loopback interface and provides the iOS USB RemoteXPC tunnel required by the app.

## Uninstalling

Deleting `Phantom.app` removes the application but not the protected helper or local settings. A future release will include an in-app uninstaller. Until then, removal of system helper components requires an administrator and should be performed only by someone comfortable managing macOS LaunchDaemons.

## Scope

Core Location simulation is not anonymity. Phantom does not alter cellular network records, IP geolocation, emergency location, physical-camera observations, phone-number records, or third-party account history.

## Route libraries, logs, and satellite

Device route libraries are stored locally in `~/.config/phantom/library.json`. Exported session logs may contain device identifiers and coordinates and stay where you save them. Optional EOX historical satellite tiles disclose requested tile areas and IP address to EOX.
