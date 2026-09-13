"""FastAPI backend for the Phantom GUI.

Exposes the engine to the renderer over one WebSocket (`/ws`):

  client -> server commands:
    {cmd:"status", udid?}                     -> {type:"status", ...readiness}
    {cmd:"set", lat, lon, udid?}              -> {type:"located", lat, lon}
    {cmd:"clear", udid?}                      -> {type:"cleared"}
    {cmd:"drive"|"fly", waypoints:[[lat,lon]], speed_mps?, dt?, jitter?,
        round_trip?, loop?, realistic?, clear_at_end?, udid?}
                                              -> {type:"route",...} then {type:"fix",...}*
                                                 then {type:"arrived"|"stopped"}
    {cmd:"stop"}                              -> {type:"stopped"}

Also serves the static renderer and a server-side OSRM route proxy (`/api/route`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import time
from collections import deque
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import Body, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import mover, routing, session, search, library, scenarios
from .geo import SPEED_PRESETS_MPS

WEBUI = Path(__file__).parent / "webui"
CONFIG_FILE = Path.home() / ".config" / "phantom" / "config.json"


EXECUTION_LOG = deque(maxlen=2000)


def number(value, name, low, high):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def coordinates(lat, lon):
    return number(lat, "latitude", -90, 90), number(lon, "longitude", -180, 180)


def waypoints_checked(points):
    if not isinstance(points, list) or not 2 <= len(points) <= 50:
        raise ValueError("route requires 2–50 waypoints")
    return [coordinates(*p) for p in points]


def device_config(udid):
    return _read_config().get("devices", {}).get(udid, {}) if udid else {}


def save_device(udid, **updates):
    if not udid:
        return
    devices = _read_config().get("devices", {})
    devices[udid] = {**devices.get(udid, {}), **updates}
    _save_config(devices=devices)


def set_virtual_location(lat, lon, udid=None):
    save_device(udid, virtual_lat=lat, virtual_lon=lon)


async def tunneld_up(host: str = "127.0.0.1", port: int = 49151) -> bool:
    """True if the RSD tunnel daemon is reachable."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.5)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True
    except Exception:
        return False


TUNNELD_LABEL = "com.phantom.tunneld"
TUNNELD_PLIST = f"/Library/LaunchDaemons/{TUNNELD_LABEL}.plist"
PRIVILEGED_RUNTIME_DIR = "/Library/PrivilegedHelperTools/com.ctwebsolutions.phantom"
PRIVILEGED_RUNTIME_EXECUTABLE = f"{PRIVILEGED_RUNTIME_DIR}/phantom-runtime"

# Static spoof keep-alive cadence (seconds). Re-asserting ~2.5x/sec keeps the
# Instruments session alive and outruns iOS's real-GPS fusion, so a "set"
# location holds instead of flickering back to the phone's true position.
HOLD_DT = 0.4


def _tunneld_plist_xml() -> str:
    """launchd plist that keeps the bundled USB tunnel helper running as root."""
    runtime = os.environ.get("PHANTOM_RUNTIME_EXECUTABLE", "").strip()
    if runtime:
        # Never execute a privileged daemon directly from the app bundle. The
        # bundle is normally writable by its owner, which would create a local
        # privilege-escalation path. start_tunneld() installs this complete
        # PyInstaller runtime into a root-owned protected directory first.
        program_args = [PRIVILEGED_RUNTIME_EXECUTABLE, "tunneld"]
    else:
        program_args = [sys.executable, "-m", "pymobiledevice3", "remote", "tunneld", "--no-wifi", "--no-mobdev2"]
    args_xml = "\n".join(f"    <string>{escape(arg)}</string>" for arg in program_args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{TUNNELD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/phantom-tunneld.log</string>
  <key>StandardErrorPath</key><string>/tmp/phantom-tunneld.log</string>
</dict>
</plist>
"""


def _packaged_runtime() -> str:
    return os.environ.get("PHANTOM_RUNTIME_EXECUTABLE", "").strip()


def tunneld_install_secure() -> bool:
    """True when a packaged build's daemon uses a protected root-owned runtime."""
    if not _packaged_runtime():
        return True
    try:
        plist_text = Path(TUNNELD_PLIST).read_text()
        helper = Path(PRIVILEGED_RUNTIME_EXECUTABLE)
        st = helper.stat()
        return (
            escape(PRIVILEGED_RUNTIME_EXECUTABLE) in plist_text
            and st.st_uid == 0
            and (st.st_mode & 0o022) == 0
        )
    except Exception:
        return False


async def start_tunneld() -> tuple[bool, str]:
    """Install + load the tunnel daemon as root via ONE macOS admin prompt.

    Uses a launchd LaunchDaemon (not nohup, which cannot detach under osascript)
    so the tunnel actually starts, stays up, and is asked for only once. The app
    then talks to it unprivileged over 127.0.0.1:49151.
    """
    # Only skip if the daemon is up, USB-only, and (for packaged builds) points
    # at a protected root-owned runtime rather than the writable app bundle.
    packaged_runtime = _packaged_runtime()
    expected_runtime = PRIVILEGED_RUNTIME_EXECUTABLE if packaged_runtime else sys.executable
    usb_only = False
    configured_runtime = False
    helper_secure = not packaged_runtime
    with contextlib.suppress(Exception):
        p = Path(TUNNELD_PLIST)
        plist_text = p.read_text() if p.exists() else ""
        usb_only = p.exists() and ("--no-wifi" in plist_text or "<string>tunneld</string>" in plist_text)
        configured_runtime = escape(expected_runtime) in plist_text
        if packaged_runtime:
            helper = Path(PRIVILEGED_RUNTIME_EXECUTABLE)
            st = helper.stat()
            helper_secure = st.st_uid == 0 and (st.st_mode & 0o022) == 0
    if usb_only and configured_runtime and helper_secure and await tunneld_up():
        return True, "already-running"

    source_dir: Optional[Path] = None
    if packaged_runtime:
        source_executable = Path(packaged_runtime).resolve()
        source_dir = source_executable.parent
        if not source_executable.is_file():
            return False, "Bundled tunnel runtime is missing. Reinstall Phantom."

    fd, tmp_name = tempfile.mkstemp(prefix=f"{TUNNELD_LABEL}-", suffix=".plist")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(_tunneld_plist_xml())

        q_plist = shlex.quote(TUNNELD_PLIST)
        q_tmp = shlex.quote(str(tmp))
        helper_install = ""
        if packaged_runtime:
            assert source_dir is not None
            q_source = shlex.quote(str(source_dir))
            q_dest = shlex.quote(PRIVILEGED_RUNTIME_DIR)
            q_exec = shlex.quote(PRIVILEGED_RUNTIME_EXECUTABLE)
            helper_install = (
                f"rm -rf {q_dest}; mkdir -p {q_dest}; "
                f"ditto {q_source} {q_dest}; chown -R root:wheel {q_dest}; "
                f"chmod -R go-w {q_dest}; chmod 755 {q_exec}; "
            )

        cmd = (
            f"launchctl bootout system {q_plist} 2>/dev/null || "
            f"launchctl unload {q_plist} 2>/dev/null || true; "
            f"{helper_install}"
            f"cp {q_tmp} {q_plist} && chown root:wheel {q_plist} && chmod 644 {q_plist} && "
            f"launchctl bootstrap system {q_plist} && "
            f"launchctl enable system/{TUNNELD_LABEL} && "
            f"launchctl kickstart -k system/{TUNNELD_LABEL}"
        )
        script = f'do shell script "{cmd}" with administrator privileges'
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        if proc.returncode != 0:
            detail = (err.decode(errors="ignore").strip() if err else "") or "cancelled"
            return False, detail
    finally:
        with contextlib.suppress(Exception):
            tmp.unlink()

    # Give launchd a moment to bring the daemon up.
    for _ in range(12):
        if await tunneld_up():
            return True, "running"
        await asyncio.sleep(1.0)
    return False, "Tunnel helper installed but did not start."


def _read_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    cfg = json.loads(CONFIG_FILE.read_text())
    if not isinstance(cfg, dict):
        raise ValueError("Phantom configuration is invalid; restore its backup before saving settings")
    return cfg


def _save_config(**updates) -> None:
    cfg = _read_config()
    cfg.update(updates)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="config-", suffix=".json", dir=CONFIG_FILE.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(cfg, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, CONFIG_FILE)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def maptiler_key() -> str:
    """MapTiler key from $PHANTOM_MAPTILER_KEY or ~/.config/phantom/config.json."""
    key = os.environ.get("PHANTOM_MAPTILER_KEY", "").strip()
    if key:
        return key
    return str(_read_config().get("maptiler_key", "")).strip()


# Per-launch shared secret. The Electron shell generates a random token, passes it
# to this backend via $PHANTOM_TOKEN, and hands the same token to the renderer via
# preload. When set, every /api/* request and the /ws control channel must present
# it — so a stray local process or a malicious web page can't drive the iPhone.
EXPECTED_TOKEN = os.environ.get("PHANTOM_TOKEN", "").strip()

# OSRM routing base is chosen SERVER-SIDE only (never from a client param — that
# was an SSRF hole). Overridable via env/config so a paid build points at a
# licensed/self-hosted router instead of the non-commercial demo server.
_ALLOWED_PROFILES = {"driving", "walking", "cycling"}


def osrm_base(profile="driving") -> str:
    if profile not in _ALLOWED_PROFILES:
        raise routing.RouteError("Unsupported routing profile")
    if profile != "driving":
        base = os.environ.get(f"PHANTOM_OSRM_{profile.upper()}_BASE", "").strip()
        base = base or str(_read_config().get(f"osrm_{profile}_base", "")).strip()
        if not base:
            raise routing.RouteError(f"Configure a {profile} routing provider or choose straight line.")
        return base
    base = os.environ.get("PHANTOM_OSRM_BASE", "").strip()
    if not base:
        base = str(_read_config().get("osrm_base", "")).strip()
    return base or routing.DEFAULT_OSRM


def _is_loopback(header_value: str) -> bool:
    """True if an Origin/Host header points at loopback (or is absent).

    Non-browser clients may omit Origin/Host; the token still gates those. A
    cross-origin page or a DNS-rebind attempt carries a non-loopback host and is
    rejected here as defense-in-depth on top of the token."""
    if not header_value:
        return True
    host = header_value.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


def _ws_subprotocol(sock) -> Optional[str]:
    """The 'phantom-<token>' subprotocol the client offered, if any."""
    for p in sock.headers.get("sec-websocket-protocol", "").split(","):
        p = p.strip()
        if p.startswith("phantom-"):
            return p
    return None


def ws_authorized(sock) -> bool:
    proto = _ws_subprotocol(sock)
    token_ok = not EXPECTED_TOKEN or bool(proto and proto[len("phantom-"):] == EXPECTED_TOKEN)
    origin_ok = _is_loopback(sock.headers.get("origin", ""))
    host_ok = _is_loopback(sock.headers.get("host", ""))
    authorized = token_ok and origin_ok and host_ok
    if not authorized:
        print(
            "[phantom] websocket rejected "
            f"token_ok={token_ok} expected_len={len(EXPECTED_TOKEN)} "
            f"offered_len={len(proto or '')} origin_ok={origin_ok} host_ok={host_ok} "
            f"origin={sock.headers.get('origin', '')!r} host={sock.headers.get('host', '')!r}",
            flush=True,
        )
    return authorized


def get_places(udid=None) -> dict:
    cfg = device_config(udid) if udid else _read_config()
    return {"bookmarks": cfg.get("bookmarks", []), "recents": cfg.get("recents", [])}


def add_recent(lat: float, lon: float, name: Optional[str] = None, udid=None) -> None:
    """Record a recently-used location (deduped, most-recent-first, capped)."""
    cfg = device_config(udid)
    recents = [
        r for r in cfg.get("recents", [])
        if not (abs(r.get("lat", 0) - lat) < 1e-6 and abs(r.get("lon", 0) - lon) < 1e-6)
    ]
    recents.insert(0, {"lat": round(lat, 6), "lon": round(lon, 6), "name": name})
    save_device(udid, recents=recents[:12])


def _readiness_dict(r: session.Readiness) -> dict:
    d = {
        "ready": r.ready,
        "tunnel_state": r.tunnel_state,
        "developer_mode": r.developer_mode,
        "ddi_state": r.ddi_state,
        "hints": r.hints,
        "device": None,
    }
    if r.device:
        d["device"] = {
            "udid": r.device.udid,
            "name": r.device.name,
            "model": r.device.model,
            "product_type": r.device.product_type,
            "ios": r.device.ios,
            "connection": r.device.connection,
        }
    return d


class ControlSession:
    """One connected GUI client."""

    def __init__(self, sock: WebSocket) -> None:
        self.sock = sock
        self._send_lock = asyncio.Lock()
        self.move_task: Optional[asyncio.Task] = None
        self.stop = asyncio.Event()
        self.live: Optional[mover.LiveRoute] = None
        self.move_mode: str = "drive"
        self.profile = "driving"
        self.udid = None
        self.state = {"state": "idle", "last_command_at": None, "last_update_at": None}
        self.last_fix = None
        self.resume_suppressed = False
        # Static-hold keep-alive: re-asserts a fixed spoof so iOS can't drift back.
        self.hold_task: Optional[asyncio.Task] = None
        self.hold_stop = asyncio.Event()

    def snapshot(self):
        return {**self.state, "device_udid": self.udid,
                "auto_resume": bool(device_config(self.udid).get("auto_resume", False))}

    async def send(self, obj: dict) -> None:
        if obj.get("type") in {"located", "fix", "arrived", "stopped", "failed", "interrupted", "paused", "resumed", "cleared"}:
            self.state.update({k: v for k, v in obj.items() if k in {"lat", "lon", "progress"}})
            if obj["type"] in {"located", "fix", "cleared"} or obj.get("cleared"):
                self.state["last_update_at"] = time.time()
            self.state["state"] = {"fix": "paused" if self.live and self.live.paused else "moving", "located": "holding", "resumed": "moving", "cleared": "idle"}.get(obj["type"], obj["type"])
        if obj.get("cleared") or obj.get("type") == "cleared":
            self.state.update(state="idle", lat=None, lon=None, progress=0)
        if obj.get("type") not in {"fix", "status"}:
            EXECUTION_LOG.append({"at": time.time(), "device_udid": self.udid, **{k: v for k, v in obj.items() if k != "points"}})
        async with self._send_lock:
            with contextlib.suppress(Exception):
                await self.sock.send_json(obj)

    async def run(self) -> None:
        try:
            while True:
                msg = await self.sock.receive_json()
                await self.dispatch(msg)
        except WebSocketDisconnect:
            pass
        finally:
            await self.cancel_move()

    async def dispatch(self, msg: dict) -> None:
        cmd = msg.get("cmd") if isinstance(msg, dict) else None
        if cmd != "status":
            self.state["last_command_at"] = time.time()
        try:
            if cmd in {"set", "clear", "drive", "fly"}:
                requested = msg.get("udid") or self.udid
                if not requested:
                    readiness = await session.diagnose(None)
                    requested = readiness.device.udid if readiness.device else None
                if not isinstance(requested, str) or not requested or len(requested) > 200:
                    raise ValueError("connect and select a device first")
                msg["udid"] = requested
                self.udid = requested
            if cmd == "status":
                active = any(task is not None and not task.done() for task in (self.move_task, self.hold_task))
                r = await session.diagnose(self.udid if active else msg.get("udid"))
                discovered = r.device.udid if r.device else None
                if not active and discovered != self.udid:
                    self.state.update(state="idle", lat=None, lon=None, progress=0, last_update_at=None)
                    self.last_fix = None
                    self.udid = discovered
                status_payload = _readiness_dict(r)
                if r.ready and not tunneld_install_secure():
                    status_payload.update(
                        ready=False,
                        tunnel_state="upgrade-required",
                        hints=["Security update required — move the USB helper into its protected system location."],
                    )
                await self.send({"type": "status", **status_payload, "session": self.snapshot()})
                if status_payload["ready"]:
                    await self._resume_virtual_if_needed(r, msg.get("udid"))
            elif cmd == "preferences":
                udid = msg.get("udid") or self.udid
                if not isinstance(udid, str) or not udid or len(udid) > 200 or not isinstance(msg.get("auto_resume"), bool):
                    raise ValueError("select a device and provide boolean auto_resume")
                save_device(udid, auto_resume=msg["auto_resume"])
                await self.send({"type": "preferences", "udid": udid, "auto_resume": msg["auto_resume"]})
            elif cmd in {"pause", "resume"}:
                if self.live is None or self.move_task is None or self.move_task.done():
                    raise ValueError("no active route")
                self.live.paused = cmd == "pause"
                self.live._last_t = time.monotonic()
                await self.send({"type": "paused" if cmd == "pause" else "resumed"})
            elif cmd == "set":
                await self._set(msg)
            elif cmd == "clear":
                await self._clear(msg)
            elif cmd in ("drive", "fly"):
                await self._start_move(msg, cmd)
            elif cmd == "stop":
                self.resume_suppressed = True
                await self.cancel_move()
                await self.send({"type": "stopped"})
            elif cmd == "set_speed":
                speed = number(msg["speed_mps"], "speed", 0.1, 10000)
                if self.live is None:
                    raise ValueError("no active route")
                self.live.speed = speed
                await self.send({"type": "speed", "speed_mps": self.live.speed})
            elif cmd == "add_stop":
                await self._add_stop(msg)
            elif cmd == "start_tunnel":
                ok, detail = await start_tunneld()
                if ok:
                    up = detail in ("already-running", "running")
                    await self.send({"type": "tunnel", "state": "up" if up else "starting"})
                else:
                    await self.send({"type": "tunnel", "state": "error", "message": detail})
            else:
                await self.send({"type": "error", "message": f"unknown command {cmd!r}"})
        except Exception as e:  # noqa: BLE001
            await self.send({"type": "error", "cmd": cmd, "message": f"{type(e).__name__}: {e}"})

    async def _resume_virtual_if_needed(self, readiness: session.Readiness, udid: Optional[str]) -> None:
        """Re-establish a persisted static hold after app/tunnel reconnection."""
        hold_active = self.hold_task is not None and not self.hold_task.done()
        move_active = self.move_task is not None and not self.move_task.done()
        if not readiness.ready or hold_active or move_active or self.resume_suppressed:
            return
        udid = readiness.device.udid if readiness.device else udid
        cfg = device_config(udid)
        if not cfg.get("auto_resume", False):
            return
        lat, lon = cfg.get("virtual_lat"), cfg.get("virtual_lon")
        if lat is None or lon is None:
            return
        await self.send({"type": "info", "message": "Resuming persistent location…"})
        await self._set({"lat": lat, "lon": lon, "udid": udid, "name": "Persistent location"})

    async def _set(self, msg: dict) -> None:
        lat, lon = coordinates(msg["lat"], msg["lon"])
        dt = number(msg.get("hold_dt", HOLD_DT), "hold interval", 0.1, 10)
        self.resume_suppressed = True
        await self.cancel_move()  # a static set must win over any running route/hold
        self.state["state"] = "idle"
        rsd, err = await session.open_ready_rsd(msg.get("udid"))
        if err:
            self.state["state"] = "idle"
            await self.send({"type": "error", "code": err[0], "message": err[1]})
            return
        lat, lon = coordinates(msg["lat"], msg["lon"])
        try:
            # Land it immediately (also lazily mounts the DDI + gives a clear error).
            await session.set_location(rsd, lat, lon)
        except Exception as e:  # noqa: BLE001
            await session._safe_close(rsd)
            await self.send({"type": "error", "message": str(e)})
            return
        self.resume_suppressed = False
        set_virtual_location(lat, lon, msg.get("udid") or self.udid)
        add_recent(lat, lon, msg.get("name"), msg.get("udid") or self.udid)
        await self.send({"type": "located", "lat": lat, "lon": lon})
        # Then HOLD it: keep re-asserting so iOS can't fuse the real GPS back in.
        # The hold task owns `rsd` for its lifetime and closes it when it ends.
        self.hold_stop = asyncio.Event()
        dt = float(msg.get("hold_dt", HOLD_DT))
        self.hold_task = asyncio.create_task(
            self._run_hold(rsd, lat, lon, dt, udid=msg.get("udid"))
        )

    async def _run_hold(self, rsd, lat: float, lon: float, dt: float, *, udid: Optional[str], stop_event=None) -> None:
        current = rsd
        reconnecting = False
        stop_event = stop_event or self.hold_stop
        def on_tick():
            nonlocal reconnecting
            self.state["last_update_at"] = time.time()
            if reconnecting:
                reconnecting = False
                asyncio.create_task(self.send({"type": "located", "lat": lat, "lon": lon, "message": "Persistent location reconnected."}))
        try:
            while not stop_event.is_set():
                if current is None:
                    current, err = await session.open_ready_rsd(udid)
                    if err:
                        if not reconnecting:
                            await self.send({"type": "interrupted", "message": "USB tunnel dropped — reconnecting…"})
                            reconnecting = True
                        await asyncio.sleep(1.0)
                        continue
                try:
                    await session.hold_location(
                        current, lat, lon, dt=dt, should_stop=stop_event.is_set, on_tick=on_tick
                    )
                except Exception:  # transient USB/RSD drops are recovered in-place
                    await session._safe_close(current)
                    current = None
                    if not reconnecting and not stop_event.is_set():
                        await self.send({"type": "interrupted", "message": "USB tunnel dropped — reconnecting…"})
                        reconnecting = True
                    if not stop_event.is_set():
                        await asyncio.sleep(1.0)
                else:
                    break
        finally:
            if current is not None:
                await session._safe_close(current)

    async def cancel_hold(self) -> None:
        """Stop the static keep-alive (if any) and let it release its rsd."""
        task = self.hold_task
        self.hold_task = None
        if task and not task.done():
            self.hold_stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=6)
            except BaseException:  # timeout/cancel: hard-stop so no orphan keeps writing
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    async def _clear(self, msg: dict) -> None:
        self.resume_suppressed = True
        self.state["state"] = "idle"
        await self.cancel_move()  # stop any running route before restoring real GPS
        rsd, err = await session.open_ready_rsd(msg.get("udid"))
        if err:
            self.state["state"] = "idle"
            await self.send({"type": "error", "code": err[0], "message": err[1]})
            return
        try:
            await session.clear_location(rsd)
            set_virtual_location(None, None, msg.get("udid") or self.udid)
            await self.send({"type": "cleared"})
        finally:
            await session._safe_close(rsd)

    async def _start_move(self, msg: dict, mode: str) -> None:
        geofences = scenarios.Geofences(msg.get("geofences"))
        waypoints = waypoints_checked(msg["waypoints"])
        speed = number(msg.get("speed_mps", SPEED_PRESETS_MPS["fly" if mode == "fly" else "drive"]), "speed", 0.1, 10000)
        dt = number(msg.get("dt", 1), "interval", 0.1, 10)
        jitter = number(msg.get("jitter", 0), "jitter", 0, 100)
        dwells = msg.get("dwell_s", [0] * len(waypoints))
        if not isinstance(dwells, list) or len(dwells) != len(waypoints):
            raise ValueError("dwell_s must align with waypoints")
        dwells = [number(v, "dwell", 0, 86400) for v in dwells]
        profile = msg.get("profile", "driving")
        if mode == "drive":
            try:
                route = await routing.road_route(waypoints, profile=profile, osrm_base=osrm_base(profile))
            except routing.RouteError as e:
                await self.send({"type": "error", "code": "routing_failed", "message": str(e)})
                return
        else:
            route = routing.straight_route(waypoints)
        await self.cancel_move()
        self.profile = profile
        self.geofences = geofences
        self.last_fix = None
        self.resume_suppressed = False
        self.waypoint_count = len(waypoints)
        rsd, err = await session.open_ready_rsd(msg.get("udid"))
        if err:
            self.state["state"] = "idle"
            await self.send({"type": "error", "code": err[0], "message": err[1]})
            return

        dwell_stops = []
        previous_index = 0
        cumulative = [0.0]
        for start, end in zip(route.points, route.points[1:]):
            cumulative.append(cumulative[-1] + mover.geo.haversine_m(*start, *end))
        for waypoint, seconds in zip(waypoints, dwells):
            index = min(range(previous_index, len(route.points)), key=lambda i: mover.geo.haversine_m(*waypoint, *route.points[i]))
            previous_index = index
            if seconds:
                if dwell_stops and dwell_stops[-1][0] == cumulative[index]:
                    dwell_stops[-1] = (cumulative[index], dwell_stops[-1][1] + seconds)
                else:
                    dwell_stops.append((cumulative[index], seconds))
        self.live = mover.LiveRoute(
            route.points, speed_mps=speed, dt=dt,
            jitter_m=jitter,
            round_trip=bool(msg.get("round_trip")), realistic=bool(msg.get("realistic")),
            loop=bool(msg.get("loop")), dwell_stops=dwell_stops,
        )
        self.move_mode = mode
        self.state["state"] = "starting"
        await self.send({
            "type": "route", "mode": mode, "points": route.points,
            "distance_m": route.distance_m, "speed_mps": speed,
        })
        self.stop = asyncio.Event()
        self.move_task = asyncio.create_task(self._run_move(rsd, msg))

    async def _add_stop(self, msg: dict) -> None:
        """Extend the live route with a new destination (mid-route)."""
        if self.live is None:
            await self.send({"type": "error", "cmd": "add_stop", "message": "no active route to extend"})
            return
        if self.waypoint_count >= 50:
            raise ValueError("route supports at most 50 waypoints")
        lat, lon = coordinates(msg["lat"], msg["lon"])
        if self.move_mode == "drive":
            try:
                leg = await routing.road_route(
                    [self.live.path[-1], (lat, lon)], profile=self.profile, osrm_base=osrm_base(self.profile)
                )
                self.live.extend(leg.points[1:] if len(leg.points) > 1 else leg.points)
            except routing.RouteError as e:
                await self.send({"type": "error", "cmd": "add_stop", "code": "routing_failed", "message": str(e)})
                return
        else:
            self.live.extend([(lat, lon)])
        self.waypoint_count += 1
        await self.send({"type": "extended", "points": self.live.path, "lat": lat, "lon": lon})

    async def _run_move(self, rsd, msg: dict) -> None:
        live = self.live
        dt = live.dt
        fixes = mover.live_fix_stream(live, self.stop.is_set)
        geofences = getattr(self, "geofences", scenarios.Geofences())
        queue: asyncio.Queue = asyncio.Queue()

        def on_fix(f) -> None:
            self.last_fix = f
            queue.put_nowait(f)

        async def pump() -> None:
            while True:
                f = await queue.get()
                if f is None:
                    return
                await self.send({
                    "type": "fix", "lat": f.lat, "lon": f.lon, "bearing": f.bearing,
                    "progress": f.progress, "eta_s": f.eta_s, "speed_mps": f.speed_mps,
                    "distance_m": f.distance_m, "total_m": f.total_m, "dwell_remaining_s": f.dwell_remaining_s,
                })
                for event in geofences.observe(f.lat, f.lon):
                    await self.send(event)

        pump_task = asyncio.create_task(pump())
        last = None
        streamed_ok = False
        try:
            try:
                last = await session.stream_fixes(
                    rsd, fixes, dt=dt, on_fix=on_fix,
                    should_stop=self.stop.is_set, clear_on_exit=False,
                )
                streamed_ok = True
            except Exception as e:  # noqa: BLE001
                await self.send({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                queue.put_nowait(None)
                with contextlib.suppress(Exception):
                    await pump_task
                last = last or self.last_fix
                clear_at_end = False
                if streamed_ok and not self.stop.is_set() and bool(msg.get("clear_at_end")):
                    try:
                        await session.clear_location(rsd)
                        clear_at_end = True
                    except Exception as e:
                        streamed_ok = False
                        await self.send({"type": "error", "message": str(e)})
                if clear_at_end:
                    set_virtual_location(None, None, msg.get("udid") or self.udid)
                elif last is not None:
                    set_virtual_location(last.lat, last.lon, msg.get("udid") or self.udid)
                if not streamed_ok:
                    self.resume_suppressed = True
                await self.send({
                    "type": "stopped" if self.stop.is_set() else ("arrived" if streamed_ok and last is not None and last.progress >= 1 else "failed"),
                    "lat": last.lat if last else None,
                    "lon": last.lon if last else None,
                    "cleared": clear_at_end,
                })
            self.live = None
            # Stay-at-end: keep re-asserting the final spot so it doesn't drift back
            # to real GPS (same keep-alive the static "set" uses). This inline hold
            # keeps the move task alive until the next cancel_move sets self.stop.
            if streamed_ok and last is not None and not self.stop.is_set() and not bool(msg.get("clear_at_end")):
                held_rsd, rsd = rsd, None
                await self._run_hold(held_rsd, last.lat, last.lon, HOLD_DT, udid=msg.get("udid") or self.udid, stop_event=self.stop)
        finally:
            if rsd is not None:
                await session._safe_close(rsd)

    async def cancel_move(self) -> None:
        # Every set/clear/route change funnels through here first, so this is also
        # where we tear down any running static hold before the next action.
        await self.cancel_hold()
        # Snapshot and detach first so a concurrent start can't target this task.
        task = self.move_task
        self.move_task = None
        if task and not task.done():
            self.stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=6)
            except BaseException:  # timeout/cancel: hard-stop so no orphan keeps writing
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self.live = None


def create_app() -> FastAPI:
    app = FastAPI(title="Phantom")
    app.include_router(search.router)
    app.include_router(library.router)

    @app.get("/api/session/log")
    async def session_log():
        return {"events": list(EXECUTION_LOG)}

    @app.middleware("http")
    async def _require_token(request, call_next):
        # Gate every /api/* route on the per-launch token (when one is set). The
        # static UI mount ("/") is exempt so the app can load and read the token.
        if EXPECTED_TOKEN and request.url.path.startswith("/api/"):
            if request.headers.get("x-phantom-token", "") != EXPECTED_TOKEN:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/api/health")
    async def health():
        # Note: no map key is echoed here — a shared key baked into a paid build
        # would be extractable and abused. The renderer defaults to keyless tiles;
        # a user's own key (if set) is applied server-side only.
        return {"ok": True, "presets": SPEED_PRESETS_MPS, "has_maptiler": bool(maptiler_key())}

    @app.get("/api/places")
    async def places(udid: Optional[str] = None):
        return get_places(udid)

    @app.post("/api/bookmark")
    async def add_bookmark(payload: dict = Body(...)):
        try:
            lat, lon = coordinates(payload["lat"], payload["lon"])
            udid = payload.get("udid")
            if not isinstance(udid, str) or not udid or len(udid) > 200:
                raise ValueError("Select a device before saving a place")
            name = str(payload.get("name") or "").strip()[:120] or f"{lat:.4f}, {lon:.4f}"
            bms = [b for b in device_config(udid).get("bookmarks", []) if b.get("name") != name]
            if len(bms) >= 500:
                raise ValueError("At most 500 saved places per device")
            bms.append({"name": name, "lat": round(lat, 6), "lon": round(lon, 6)})
            save_device(udid, bookmarks=bms)
            return {"ok": True, "bookmarks": bms}
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/unbookmark")
    async def del_bookmark(payload: dict = Body(...)):
        try:
            udid = payload.get("udid")
            if not isinstance(udid, str) or not udid or len(udid) > 200:
                raise ValueError("Select a device before removing a place")
            bms = device_config(udid).get("bookmarks", [])
            i = int(payload.get("index", -1))
            if not 0 <= i < len(bms):
                raise ValueError("Saved place no longer exists")
            bms.pop(i)
            save_device(udid, bookmarks=bms)
            return {"ok": True, "bookmarks": bms}
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/route")
    async def api_route(
        coords: str = Query(..., description="lat,lon;lat,lon;..."),
        profile: str = "driving",
    ):
        # Router base is server-chosen (osrm_base), never client-supplied — a
        # client-controlled URL here was an SSRF sink. profile is allowlisted.
        try:
            pts = waypoints_checked([tuple(map(float, p.split(","))) for p in coords.split(";") if p.strip()])
            r = await routing.road_route(pts, profile=profile, osrm_base=osrm_base(profile))
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except routing.RouteError as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        return {"points": r.points, "distance_m": r.distance_m, "duration_s": r.duration_s}

    @app.get("/api/where")
    async def where(udid: Optional[str] = None):
        """The device's current VIRTUAL (spoofed) location — never the Mac's GPS."""
        cfg = device_config(udid)
        if cfg.get("virtual_lat") is not None and cfg.get("virtual_lon") is not None:
            return {"lat": cfg["virtual_lat"], "lon": cfg["virtual_lon"], "source": "virtual"}
        return {"lat": None, "lon": None, "source": "none"}

    @app.post("/api/devenable")
    async def api_devenable(payload: dict = Body(default=None)):
        """Enable Developer Mode on the connected iPhone (triggers iOS prompt like 3uTools)."""
        udid = (payload or {}).get("udid")
        rsd, err = await session.open_ready_rsd(udid)
        if err:
            return JSONResponse({"success": False, "error": err[1]}, status_code=400)

        progress_messages = []
        def progress(msg: str):
            progress_messages.append(msg)

        try:
            success, message = await session.enable_developer_mode(rsd, progress_callback=progress)
            return {
                "success": success,
                "message": message,
                "progress": progress_messages
            }
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"success": False, "error": f"{type(e).__name__}: {e}", "progress": progress_messages},
                status_code=500
            )
        finally:
            await session._safe_close(rsd)

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        # Reject before accept() if the token/origin don't check out, so an
        # unauthorized caller never reaches the command dispatcher.
        if not ws_authorized(sock):
            await sock.close(code=1008)  # policy violation
            return
        await sock.accept(subprotocol=_ws_subprotocol(sock))
        await ControlSession(sock).run()

    if WEBUI.exists():
        app.mount("/", StaticFiles(directory=str(WEBUI), html=True), name="webui")

    return app


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    # The control plane drives a physical device; never expose it beyond loopback.
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"refusing to bind the control plane to non-loopback host {host!r}")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="phantom-server", description="Phantom GUI backend")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    run(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
