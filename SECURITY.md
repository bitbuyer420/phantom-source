# Phantom Security

## Architecture

- Electron renderer uses `contextIsolation: true`, sandboxing, and no Node integration.
- Backend binds only to loopback on a random port.
- Every launch creates a random 192-bit control token shared with the renderer through the preload bridge.
- `/api/*` and the WebSocket control channel enforce the token; WebSocket Host and Origin must resolve to loopback.
- Route-provider URLs are server-controlled to prevent client-driven SSRF.
- The privileged USB tunnel runtime is copied to `/Library/PrivilegedHelperTools/com.ctwebsolutions.phantom`, owned by root, with group/world write permissions removed.
- The LaunchDaemon executes only that protected copy—not code from the user-writable app bundle.

## Distribution status

Version 1.0.0 is ad-hoc signed and SHA-256 checksummed. It is **not Apple-notarized** because no Developer ID certificate is configured. macOS recipients must Control-click the app and choose Open once. Users should never disable Gatekeeper globally.

## Network dependencies

OpenFreeMap, Nominatim, and OSRM receive map, search, or route requests. Phantom has no first-party telemetry endpoint.

## Reporting issues

Do not publish device identifiers, private coordinates, pairing records, or logs containing personal data. Report reproducible security issues through the project download page's support link.

## Responsible use

Phantom is intended for development and QA on devices the operator owns or is authorized to test. It is not designed to conceal carrier location, defeat emergency services, falsify physical observations, or impersonate phone numbers.
