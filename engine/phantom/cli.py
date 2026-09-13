"""Phantom CLI — Milestone 1 proof and everyday driver for the engine.

Commands:
  phantom doctor                 readiness check (device, trust, tunnel, dev mode, DDI)
  phantom info                   print device info
  phantom devices                list connected devices
  phantom tunnel                 print the exact sudo command to start the tunnel daemon
  phantom set LAT LON            teleport to a static point
  phantom clear                  stop spoofing, restore real GPS
  phantom drive --from LAT,LON --to LAT,LON [--speed drive | --mph N] [opts]
  phantom fly   --from LAT,LON --to LAT,LON [--speed fly | --mph N] [opts]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

from . import routing, session
from .geo import SPEED_PRESETS_MPS
from .mover import iter_route_fixes

MPS_TO_MPH = 2.2369362920544
_ISATTY = sys.stdout.isatty()
_COLORS = {"green": "92", "red": "91", "yellow": "93", "cyan": "96", "dim": "2", "bold": "1"}


def c(text: str, color: str) -> str:
    if not _ISATTY:
        return text
    return f"\033[{_COLORS.get(color, '0')}m{text}\033[0m"


def out(text: str = "") -> None:
    print(text)


def tunnel_command() -> str:
    return f"sudo {sys.executable} -m pymobiledevice3 remote tunneld"


# ----------------------------- parsing helpers -----------------------------

def _parse_latlon(s: str) -> tuple[float, float]:
    parts = s.replace(" ", "").split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected 'lat,lon', got {s!r}")
    return float(parts[0]), float(parts[1])


def _waypoints(args) -> list[tuple[float, float]]:
    if getattr(args, "route", None):
        return [_parse_latlon(p) for p in args.route.split(";") if p.strip()]
    wps: list[tuple[float, float]] = []
    if getattr(args, "start", None):
        wps.append(_parse_latlon(args.start))
    if getattr(args, "end", None):
        wps.append(_parse_latlon(args.end))
    return wps


def _resolve_speed(args, default_preset: str) -> float:
    if getattr(args, "mph", None) is not None:
        return args.mph / MPS_TO_MPH
    if getattr(args, "kmh", None) is not None:
        return args.kmh / 3.6
    if getattr(args, "mps", None) is not None:
        return args.mps
    return SPEED_PRESETS_MPS[args.speed or default_preset]


# ----------------------------- rendering -----------------------------

def _print_device(info: session.DeviceInfo) -> None:
    out(c("Device Information", "bold"))
    out(f"  Name:         {info.name}")
    out(f"  Model:        {info.model}  ({info.product_type})")
    out(f"  iOS Version:  {info.ios}")
    out(f"  UDID:         {info.udid}")
    out(f"  Connection:   {info.connection}")


def _yesno(v) -> str:
    if v is True:
        return c("Yes", "green")
    if v is False:
        return c("No", "red")
    return c("Unknown", "yellow")


_TUNNEL_LABEL = {
    "ok": c("Connected", "green"),
    "tunneld-down": c("Daemon not running", "red"),
    "no-tunnel-for-device": c("No tunnel for device", "yellow"),
    "error": c("Error", "red"),
    "unknown": c("Unknown", "yellow"),
}
_DDI_LABEL = {
    "mounted": c("Mounted", "green"),
    "already": c("Mounted", "green"),
    "dev-mode-off": c("Blocked (Dev Mode off)", "red"),
    "not-found": c("Not available", "red"),
    "error": c("Error", "red"),
    "unknown": c("Unknown", "yellow"),
}


# ----------------------------- rsd acquisition -----------------------------

async def _open_ready_rsd(udid):
    """Acquire a tunnel-connected, DDI-mounted RSD. Returns (rsd, error_message)."""
    rsd, err = await session.open_ready_rsd(udid)
    if err is None:
        return rsd, None
    code, message = err
    if code == "tunneld-down":
        message += "\n    " + tunnel_command()
    return None, message


# ----------------------------- commands -----------------------------

async def cmd_devices(args) -> int:
    devices = await session.list_ios_devices()
    if not devices:
        out(c("No devices connected.", "yellow"))
        return 1
    out(c(f"{len(devices)} device(s):", "bold"))
    for d in devices:
        out(f"  {d.serial}  ({getattr(d, 'connection_type', '?')})")
    return 0


async def cmd_devenable(args) -> int:
    """Enable Developer Mode on the connected iPhone (triggers iOS prompt like 3uTools)."""
    rsd, err = await _open_ready_rsd(args.udid)
    if err:
        out(c("✗ " + err, "red"))
        return 1

    def progress(msg: str) -> None:
        out(c(f"  → {msg}", "cyan"))

    out(c("Enabling Developer Mode on iPhone...", "bold"))
    out("")

    try:
        success, message = await session.enable_developer_mode(rsd, progress_callback=progress)
        if success:
            out(c(f"✓ {message}", "green"))
        else:
            out(c(f"✗ {message}", "red"))
            return 1
    except Exception as e:  # noqa: BLE001
        out(c(f"✗ {type(e).__name__}: {e}", "red"))
        return 1
    finally:
        await session._safe_close(rsd)
    return 0


async def cmd_info(args) -> int:
    try:
        info = await session.read_device_info(args.udid)
    except Exception as e:  # noqa: BLE001
        out(c(f"✗ {type(e).__name__}: {e}", "red"))
        return 1
    _print_device(info)
    return 0


async def cmd_tunnel(args) -> int:
    out("Start the RSD tunnel daemon once per session (needs your Mac password):")
    out("    " + c(tunnel_command(), "cyan"))
    out("")
    out(c("Leave it running in its own terminal; then use phantom set/drive/fly here.", "dim"))
    return 0


async def cmd_doctor(args) -> int:
    r = await session.diagnose(args.udid)
    if r.device:
        _print_device(r.device)
    else:
        out(c("No device detected.", "red"))
    out("")
    out(c("Device Status", "bold"))
    out(f"  Tunnel:          {_TUNNEL_LABEL.get(r.tunnel_state, r.tunnel_state)}")
    out(f"  Developer Mode:  {_yesno(r.developer_mode)}")
    out(f"  DDI Mounted:     {_DDI_LABEL.get(r.ddi_state, r.ddi_state)}")
    out(f"  Status:          {c('READY', 'green') if r.ready else c('NOT READY', 'yellow')}")
    if r.hints:
        out("")
        for h in r.hints:
            out(c("→ ", "cyan") + h)
    if r.tunnel_state != "ok":
        out("")
        out(c("Start the tunnel daemon (one time, needs your Mac password):", "dim"))
        out("    " + c(tunnel_command(), "cyan"))
    return 0 if r.ready else 2


async def cmd_set(args) -> int:
    rsd, err = await _open_ready_rsd(args.udid)
    if err:
        out(c("✗ " + err, "red"))
        return 1
    try:
        await session.set_location(rsd, args.lat, args.lon)
        out(c(f"✓ Location set to {args.lat:.6f}, {args.lon:.6f}", "green"))
        out(c("  Persists on the device until 'phantom clear'.", "dim"))
    except Exception as e:  # noqa: BLE001
        out(c(f"✗ {type(e).__name__}: {e}", "red"))
        return 1
    finally:
        await session._safe_close(rsd)
    return 0


async def cmd_clear(args) -> int:
    rsd, err = await _open_ready_rsd(args.udid)
    if err:
        out(c("✗ " + err, "red"))
        return 1
    try:
        await session.clear_location(rsd)
        out(c("✓ Cleared — real GPS restored.", "green"))
    except Exception as e:  # noqa: BLE001
        out(c(f"✗ {type(e).__name__}: {e}", "red"))
        return 1
    finally:
        await session._safe_close(rsd)
    return 0


async def _cmd_move(args, mode: str) -> int:
    wps = _waypoints(args)
    if len(wps) < 2:
        out(c("✗ Need at least two points: --from LAT,LON --to LAT,LON (or --route).", "red"))
        return 1

    speed = _resolve_speed(args, "fly" if mode == "fly" else "drive")

    if mode == "drive":
        try:
            route = await routing.road_route(wps, osrm_base=args.osrm)
        except routing.RouteError as e:
            out(c(f"✗ Routing failed: {e}", "red"))
            return 1
    else:
        route = routing.straight_route(wps)

    pts = route.points
    total_km = (route.distance_m or 0) / 1000
    out(f"{mode.title()}: {len(pts)} points · {total_km:.2f} km · ~{speed * MPS_TO_MPH:.0f} mph"
        + (" · round-trip" if args.round_trip else "") + (" · loop" if args.loop else ""))

    rsd, err = await _open_ready_rsd(args.udid)
    if err:
        out(c("✗ " + err, "red"))
        return 1

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, stop.set)

    fixes = iter_route_fixes(
        pts, speed, dt=args.dt, jitter_m=args.jitter,
        round_trip=args.round_trip, loop=args.loop, realistic=args.realistic,
    )

    def on_fix(f) -> None:
        eta_m, eta_s = int(f.eta_s // 60), int(f.eta_s % 60)
        sys.stdout.write(
            f"\r  {f.lat:.5f},{f.lon:.5f}  {f.speed_mps * MPS_TO_MPH:5.1f} mph  "
            f"{f.progress * 100:5.1f}%  ETA {eta_m:02d}:{eta_s:02d}  hdg {f.bearing:3.0f}°   "
        )
        sys.stdout.flush()

    try:
        last = await session.stream_fixes(
            rsd, fixes, dt=args.dt, on_fix=on_fix,
            should_stop=stop.is_set, clear_on_exit=args.clear_at_end,
        )
        sys.stdout.write("\n")
        out(c("■ Stopped.", "yellow") if stop.is_set() else c("✓ Arrived.", "green"))
        if not args.clear_at_end and last is not None:
            out(c(f"  Holding at {last.lat:.6f},{last.lon:.6f} — 'phantom clear' to restore real GPS.", "dim"))
    except Exception as e:  # noqa: BLE001
        sys.stdout.write("\n")
        out(c(f"✗ {type(e).__name__}: {e}", "red"))
        return 1
    finally:
        await session._safe_close(rsd)
    return 0


async def cmd_drive(args) -> int:
    return await _cmd_move(args, "drive")


async def cmd_fly(args) -> int:
    return await _cmd_move(args, "fly")


# ----------------------------- parser -----------------------------

def _add_move_opts(p: argparse.ArgumentParser, default_speed: str) -> None:
    p.add_argument("--from", dest="start", metavar="LAT,LON", help="start point")
    p.add_argument("--to", dest="end", metavar="LAT,LON", help="end point")
    p.add_argument("--route", metavar="LAT,LON;LAT,LON;...", help="explicit multi-point route")
    p.add_argument("--speed", choices=list(SPEED_PRESETS_MPS), default=None,
                   help=f"speed preset (default: {default_speed})")
    p.add_argument("--mph", type=float, help="speed in mph (overrides preset)")
    p.add_argument("--kmh", type=float, help="speed in km/h (overrides preset)")
    p.add_argument("--mps", type=float, help="speed in m/s (overrides preset)")
    p.add_argument("--dt", type=float, default=1.0, help="seconds between GPS updates (default 1.0)")
    p.add_argument("--jitter", type=float, default=0.0, metavar="M", help="max metres of positional wobble")
    p.add_argument("--round-trip", action="store_true", help="travel out and back")
    p.add_argument("--loop", action="store_true", help="repeat forever until Ctrl-C")
    p.add_argument("--realistic", action="store_true", help="vary speed +/-15%% and apply jitter")
    p.add_argument("--clear-at-end", action="store_true", help="restore real GPS when finished")
    p.add_argument("--udid", help="target a specific device UDID")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phantom", description="Phantom — GPS location controller for a USB-tethered iPhone (macOS).")
    sub = parser.add_subparsers(dest="command")

    sp = sub.add_parser("doctor", help="readiness check")
    sp.add_argument("--udid")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("info", help="print device info")
    sp.add_argument("--udid")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("devices", help="list connected devices")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("devenable", help="enable Developer Mode on iPhone (triggers iOS prompt like 3uTools)")
    sp.add_argument("--udid", help="target a specific device UDID")
    sp.set_defaults(func=cmd_devenable)

    sp = sub.add_parser("tunnel", help="print the sudo command to start the tunnel daemon")
    sp.set_defaults(func=cmd_tunnel)

    sp = sub.add_parser("set", help="teleport to a static point")
    sp.add_argument("lat", type=float)
    sp.add_argument("lon", type=float)
    sp.add_argument("--udid")
    sp.set_defaults(func=cmd_set)

    sp = sub.add_parser("clear", help="stop spoofing, restore real GPS")
    sp.add_argument("--udid")
    sp.set_defaults(func=cmd_clear)

    sp = sub.add_parser("drive", help="animate a road route")
    _add_move_opts(sp, "drive")
    sp.add_argument("--osrm", default=routing.DEFAULT_OSRM, help="OSRM base URL")
    sp.set_defaults(func=cmd_drive)

    sp = sub.add_parser("fly", help="animate a straight great-circle flight")
    _add_move_opts(sp, "fly")
    sp.set_defaults(func=cmd_fly)

    sp = sub.add_parser("serve", help="run the GUI backend server")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) == "serve":
        from . import server
        out(c(f"Phantom backend on http://{args.host}:{args.port}  (Ctrl-C to stop)", "cyan"))
        server.run(args.host, args.port)
        return 0
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
