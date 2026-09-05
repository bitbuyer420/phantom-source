# Phantom 👻

A sleek macOS desktop app that overrides the GPS location of a **USB-tethered iPhone** — teleport to any point, or animate realistic movement along a road (**drive**) or through the sky (**fly**). Built on Apple's own developer location-simulation mechanism via [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3). **Nothing is installed on the phone; no jailbreak.**

Built by [CT Web Solutions](https://ctwebsolutions.com).

## Download

Grab the latest `.dmg` from [Releases](../../releases). Apple Silicon (arm64) only.

> The app is not notarized — on first launch, right-click the app → **Open** → **Open**.

## Requirements

- macOS (Apple Silicon), iPhone on iOS 17+ with Developer Mode enabled and trusted over USB
- Python 3.13 + OpenSSL (for the tunnel runtime; the app guides setup)

## Responsible use

Phantom only changes what *your own* device reports. Spoofing location can violate some apps' terms of service. Use responsibly.
