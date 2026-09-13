PHANTOM 1.0.0 — INSTALLATION

Requirements
- Apple Silicon Mac running macOS 11 or later
- iPhone with iOS 17.4 or later
- Developer Mode enabled on the iPhone
- USB data cable and “Trust This Computer” accepted

Install
1. Drag Phantom.app to Applications.
2. Because this community build is not Apple-notarized, Control-click Phantom in Applications and choose Open the first time. Do not disable Gatekeeper globally.
3. Connect and unlock your iPhone.
4. Click Enable (or Secure & Enable) and approve the macOS administrator prompt. Phantom installs its USB tunnel runtime into a protected root-owned system directory.
5. Use Reset before disconnecting when you want to restore real GPS.

Security
- The control server binds only to 127.0.0.1 and uses a new random token each launch.
- The privileged helper is installed under /Library/PrivilegedHelperTools and is not user-writable.
- Phantom contains no analytics, advertising SDK, account system, or shared API key.
- Map tiles, place searches, and route waypoints are sent to third-party mapping services. See PRIVACY.md on the download site.

Important limitations
- Phantom changes app-visible Core Location on a connected iPhone for development and QA.
- It does not alter carrier records, cell-tower location, IP geolocation, emergency location, Find My guarantees, physical camera observations, or phone-number information.
- Use only with devices you own or are authorized to test. Follow application terms and local law.

Integrity
Compare the SHA-256 shown on the official download page with:
  shasum -a 256 ~/Downloads/Phantom-1.0.0-arm64.dmg

Support
Download page: https://phantom-location.vercel.app
