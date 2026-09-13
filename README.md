# Phantom

macOS location-testing app for a USB-connected iPhone, built by CT Web Solutions.

[Download Phantom 1.1.0 for Apple Silicon](https://phantom-location.vercel.app/) · [Release notes](RELEASE_NOTES_1.1.0.md)

The download bundles its Python runtime. No Python installation is required for the packaged app. The app is ad-hoc signed, not Apple-notarized. A trusted iPhone with Developer Mode is required; USB compatibility must be checked on the target iOS version.

## Development

Use Node.js 22+, Python 3.13+ with OpenSSL, and uv.

```sh
cd engine
uv sync --locked
cd ../app
npm ci
npm start
```

Run `bash scripts/test.sh` from the repository root, or `npm test` from app for frontend/lifecycle checks. Tests mock iPhone I/O.

## Build on Apple Silicon macOS

```sh
uv pip install --python engine/.venv/bin/python pyinstaller
cd app
npm run build:mac
```

Builds appear in `dist/`. No credentials, device data, virtual environments, or generated app bundles are stored in this repository.

## Features and providers

Pause/resume routes, per-device libraries, GPX import/export, timed stops, session logs, and sampled geofences. Map/Satellite uses historical EOX 2016/2017 imagery with attribution. See release notes for map behavior, provider configuration, and limitations.

Public search/routing services have usage limits. Distributed deployments must configure suitable providers. Walking/cycling road routing requires separately configured endpoints. No telemetry or analytics is added.
