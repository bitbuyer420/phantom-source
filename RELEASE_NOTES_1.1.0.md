# Phantom 1.1.0

This release improves session reliability and adds reusable location-testing scenarios.

## Changes

- Distinct failed, interrupted, stopped, and arrived states; restoring GPS reports success only after the clear command succeeds.
- Persistent disconnection and error messages. Commands cannot silently disappear while disconnected.
- Pause/resume preserves in-process route progress and holds the confirmed coordinate. Stop ends movement; Restore Real GPS clears simulated location.
- Automatic restoration of saved static locations is off by default and enabled separately for each device. Legacy global locations are preserved but never automatically applied to another device.
- Separate device route libraries, bookmarks, recents, and resume preferences.
- Route/scenario libraries support names, folders, duplication, waypoint editing/reordering, speed/options, timed stops, and GPX import/export.
- Optional circular geofences around waypoints produce sampled entry/exit events in the execution log.
- Road errors preserve the plan and offer an explicit straight-line alternative. Walking/cycling do not silently use driving routes.
- Submitted search replaces autocomplete; a local proxy caches results and limits request frequency.
- Brighter map, accessible control labels, keyboard search navigation, session dashboard, clearer setup guidance, and bounded high-speed testing settings.
- Consistent system typography across controls and map chrome, matching translucent panels and neutral form fields, plus polished waypoint labels and action spacing. Advanced speed matches adjacent controls at 14px/500. Versioned assets prevent stale theme caches.
- One Electron engine per application instance. Closing the macOS window hides it; use Quit to stop the engine. Unexpected engine exits offer recovery.

## Use

Choose Route, add waypoints on the map or through coordinate/search entry, and set each stop's duration in seconds. Save the scenario with a name and optional folder. Load it later for repeatable testing. Preview roads before starting. Pause holds position; Resume continues the remaining route.

GPX import accepts 2–50 route, track, or waypoint points and files under 2 MB. It rejects larger tracks rather than silently changing them. GPX carries coordinates; save in the route library to preserve Phantom-specific scenario settings. Geofence events describe confirmed sampled fixes, not another app's notification delivery; small fences can be crossed between samples.

Route progress survives Pause/Resume in the same running session. After an engine/app restart, opt-in recovery restores the last saved static position, not a running route. Route libraries remain saved across restarts. Execution logs contain a bounded history for the current engine run and can be exported locally.

## Providers

Public Nominatim is limited to modest personal use. Phantom sends only explicit submitted searches with a one-hour in-memory cache and at least 1.1 seconds between requests within an engine. The public limit applies across all users of an application, so public/distributed use requires a provider with suitable capacity. See https://operations.osmfoundation.org/policies/nominatim/.

Configure a Nominatim-compatible HTTPS search endpoint using `geocoder_url` in `~/.config/phantom/config.json` (or `PHANTOM_GEOCODER_URL`). This does not require rebuilding the app. No provider subscription or credentials were added by this update.

Driving uses `osrm_base` / `PHANTOM_OSRM_BASE`. Walking and cycling require dedicated `osrm_walking_base` / `PHANTOM_OSRM_WALKING_BASE` and `osrm_cycling_base` / `PHANTOM_OSRM_CYCLING_BASE` endpoints with the correct routing data. If absent, Phantom explains the limitation and offers explicit straight-line movement.

## Verification scope

Automated regressions mock device I/O and external search. A connected iPhone is still needed for end-to-end USB, real GPS restoration, and OS-version compatibility checks. The local macOS build uses ad-hoc signing; this release does not add Apple notarization.

## Map updates

- Settlement names and dots are hidden below zoom 8 and appear at closer zoom. State and country names remain visible.
- Missing terrain textures no longer render as repeating dots.
- Show main highways hides motorway/trunk overlays without changing routing.
- Map / Satellite preserves camera, pins, routes, and highway visibility. Satellite uses historical EOX Sentinel-2 imagery from 2016/2017, with CC BY 4.0 attribution; it is not current aerial imagery.
- Map-view and highway controls reset to Map / visible on a new launch.
