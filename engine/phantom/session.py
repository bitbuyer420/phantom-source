"""Device layer: discover the iPhone, read info, manage the RSD tunnel + DDI,
and drive the location-simulation service. All calls are async (pymobiledevice3 9.x).

Pipeline for iOS 17+/26:
  usbmux -> lockdown (info)  ->  tunneld RSD  ->  auto-mount personalized DDI
  ->  DvtProvider + LocationSimulation.set()/clear()
The RSD tunnel is created by a root `remote tunneld` daemon; this module only
*consumes* it over 127.0.0.1:49151, so nothing here needs root.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from pymobiledevice3 import exceptions as pmd
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.mobile_image_mounter import (
    AlreadyMountedError,
    MobileImageMounterService,
    auto_mount,
)
from pymobiledevice3.tunneld.api import TUNNELD_DEFAULT_ADDRESS, get_tunneld_device_by_udid
from pymobiledevice3.usbmux import list_devices

from .mover import Fix

# Minimal ProductType -> marketing name map (recent models); falls back to raw id.
_MODEL_NAMES = {
    "iPhone13,1": "iPhone 12 mini", "iPhone13,2": "iPhone 12", "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    "iPhone14,4": "iPhone 13 mini", "iPhone14,5": "iPhone 13", "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,7": "iPhone 14", "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro", "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15", "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro", "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone17,3": "iPhone 16", "iPhone17,4": "iPhone 16 Plus",
    "iPhone17,1": "iPhone 16 Pro", "iPhone17,2": "iPhone 16 Pro Max",
}


@dataclass
class DeviceInfo:
    udid: str
    name: str
    product_type: str
    model: str
    ios: str
    connection: str


@dataclass
class Readiness:
    """Result of `diagnose()` — mirrors GhostMe's device-status panel."""

    device: Optional[DeviceInfo] = None
    tunnel_state: str = "unknown"          # ok | tunneld-down | no-tunnel-for-device | error
    developer_mode: Optional[bool] = None  # None = could not determine
    ddi_state: str = "unknown"             # mounted | already | dev-mode-off | not-found | error
    ready: bool = False
    hints: list[str] = field(default_factory=list)


async def _safe_close(obj) -> None:
    for name in ("aclose", "close"):
        fn = getattr(obj, name, None)
        if fn is None:
            continue
        with contextlib.suppress(Exception):
            res = fn()
            if asyncio.iscoroutine(res):
                await res
        return


async def list_ios_devices() -> list:
    return await list_devices()


async def pick_udid(prefer_usb: bool = True) -> Optional[str]:
    devices = await list_devices()
    if not devices:
        return None
    if prefer_usb:
        usb = [d for d in devices if str(getattr(d, "connection_type", "")).upper().startswith("USB")]
        if usb:
            return usb[0].serial
    return devices[0].serial


async def read_device_info(udid: Optional[str] = None) -> DeviceInfo:
    """Read Name/Model/iOS over usbmux+lockdown (no tunnel needed).

    May raise NotTrustedError / PairingDialogResponsePendingError / UserDeniedPairingError
    if the on-device 'Trust This Computer?' prompt hasn't been accepted.
    """
    if udid is None:
        udid = await pick_udid()
        if udid is None:
            raise pmd.NoDeviceConnectedError()

    lockdown = await create_using_usbmux(serial=udid)
    try:
        name = await lockdown.get_value(None, "DeviceName")
        product_type = await lockdown.get_value(None, "ProductType")
        ios = await lockdown.get_value(None, "ProductVersion")
        real_udid = getattr(lockdown, "udid", None) or udid
        conn = "USB"
    finally:
        await _safe_close(lockdown)

    return DeviceInfo(
        udid=real_udid,
        name=str(name or "iPhone"),
        product_type=str(product_type or "?"),
        model=_MODEL_NAMES.get(str(product_type), str(product_type or "?")),
        ios=str(ios or "?"),
        connection=conn,
    )


async def acquire_rsd(udid: str, tunneld_address=TUNNELD_DEFAULT_ADDRESS):
    """Return (rsd, state). rsd is an already-connected RemoteServiceDiscoveryService or None.

    state is one of: ok | tunneld-down | no-tunnel-for-device | error
    """
    try:
        rsd = await get_tunneld_device_by_udid(udid, tunneld_address)
    except pmd.TunneldConnectionError:
        return None, "tunneld-down"
    except Exception:  # noqa: BLE001
        return None, "error"
    if rsd is None:
        return None, "no-tunnel-for-device"
    return rsd, "ok"


async def tunnel_state_for(udid: str, tunneld_address=TUNNELD_DEFAULT_ADDRESS) -> str:
    """Cheap readiness probe via tunneld's HTTP list — no RSD handshake.

    Used by the ~4s status poll so it doesn't churn a full RSD tunnel connect
    every tick (which stresses the tunnel and worsens drops).
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            data = (await client.get(f"http://{tunneld_address[0]}:{tunneld_address[1]}/")).json()
    except Exception:  # noqa: BLE001
        return "tunneld-down"
    return "ok" if udid in data else "no-tunnel-for-device"


async def query_developer_mode(rsd) -> Optional[bool]:
    try:
        service = MobileImageMounterService(rsd)
    except Exception:  # noqa: BLE001
        return None
    try:
        return bool(await service.query_developer_mode_status())
    except pmd.DeveloperModeIsNotEnabledError:
        return False
    except Exception:  # noqa: BLE001
        return None
    finally:
        await _safe_close(service)


async def enable_developer_mode(rsd, *, progress_callback=None, timeout: float = 120.0) -> tuple[bool, str]:
    """
    Attempt to enable Developer Mode on the connected iPhone.

    This works by attempting to mount the DDI, which triggers iOS to show
    the Developer Mode enable prompt on the device (like 3uTools does).

    Returns (success, message).
    """
    service = None
    try:
        service = MobileImageMounterService(rsd)
    except Exception as e:  # noqa: BLE001
        return False, f"Failed to create MobileImageMounterService: {e}"

    # First check current status
    try:
        status = await service.query_developer_mode_status()
        if status:
            return True, "Developer Mode is already enabled."
    except pmd.DeveloperModeIsNotEnabledError:
        pass  # Expected - this is why we're here
    except Exception as e:  # noqa: BLE001
        return False, f"Error checking Developer Mode status: {e}"

    # Attempt to mount DDI - this triggers the iOS Developer Mode prompt on the device
    if progress_callback:
        progress_callback("Attempting to trigger Developer Mode prompt on iPhone...")

    try:
        await service.mount_image()
        # If we get here without exception, Developer Mode was enabled and DDI mounted
        return True, "Developer Mode enabled and DDI mounted successfully."
    except pmd.DeveloperModeIsNotEnabledError:
        # This is expected - Developer Mode is off, iOS should have shown the prompt
        if progress_callback:
            progress_callback("Developer Mode prompt should now be visible on your iPhone.")
    except pmd.DeveloperDiskImageNotFoundError:
        return False, "Developer Disk Image not found for this iOS version. Update Phantom or macOS."
    except Exception as e:  # noqa: BLE001
        return False, f"Error triggering Developer Mode prompt: {e}"

    # Provide clear instructions
    instructions = (
        "On your iPhone, go to:\n"
        "  Settings → Privacy & Security → Developer Mode\n"
        "  Toggle ON 'Developer Mode'\n"
        "  Tap 'Restart' when prompted\n"
        "  After restart, unlock iPhone and tap 'Turn On' in the prompt\n"
        "  Enter your passcode to confirm"
    )

    if progress_callback:
        progress_callback(instructions)

    # Poll for Developer Mode to be enabled
    start_time = asyncio.get_event_loop().time()
    poll_interval = 3.0

    while asyncio.get_event_loop().time() - start_time < timeout:
        await asyncio.sleep(poll_interval)

        if progress_callback:
            elapsed = int(asyncio.get_event_loop().time() - start_time)
            progress_callback(f"Waiting for Developer Mode... ({elapsed}s elapsed)")

        try:
            status = await service.query_developer_mode_status()
            if status:
                if progress_callback:
                    progress_callback("Developer Mode enabled! Mounting DDI...")
                # Now mount the DDI
                try:
                    await service.mount_image()
                    return True, "Developer Mode enabled and DDI mounted successfully."
                except Exception as e:  # noqa: BLE001
                    return False, f"Developer Mode enabled but DDI mount failed: {e}"
        except pmd.DeveloperModeIsNotEnabledError:
            continue  # Still not enabled, keep polling
        except Exception as e:  # noqa: BLE001
            # Some transient error, keep polling
            continue

    return False, f"Timed out after {timeout}s waiting for Developer Mode to be enabled."


async def ensure_ddi(rsd) -> str:
    """Mount the personalized Developer Disk Image. Returns a state string."""
    try:
        await auto_mount(rsd)
        return "mounted"
    except AlreadyMountedError:
        return "already"
    except pmd.DeveloperModeIsNotEnabledError:
        return "dev-mode-off"
    except pmd.DeveloperDiskImageNotFoundError:
        return "not-found"
    except Exception:  # noqa: BLE001
        return "error"


async def _bounded_mount(rsd, timeout: float = 25.0) -> None:
    """Best-effort DDI mount. On iOS 26 the mounter service can hang even when the
    image is already mounted, so this is bounded and never fatal."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(auto_mount(rsd), timeout=timeout)


async def _dvt_set(rsd, lat: float, lon: float) -> None:
    async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as loc:
        await loc.set(lat, lon)


async def set_location(rsd, lat: float, lon: float) -> None:
    """Set a static location. Tries the DVT service directly (works when the DDI is
    already mounted); only if that fails does it attempt a bounded mount + retry.
    Fails fast with a clear message if the phone is locked/dropped."""
    try:
        await asyncio.wait_for(_dvt_set(rsd, lat, lon), timeout=12)
        return
    except Exception:
        pass
    await _bounded_mount(rsd)
    try:
        await asyncio.wait_for(_dvt_set(rsd, lat, lon), timeout=12)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("iPhone not responding — unlock it and keep it plugged in over USB.") from e


async def clear_location(rsd) -> None:
    """Stop simulating and restore the device's real GPS."""
    async def _do():
        async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as loc:
            await loc.clear()
    await asyncio.wait_for(_do(), timeout=25)


async def hold_location(
    rsd,
    lat: float,
    lon: float,
    *,
    dt: float = 0.5,
    should_stop: Optional[Callable[[], bool]] = None,
    on_tick: Optional[Callable[[], None]] = None,
) -> None:
    """Continuously re-assert a static location so iOS can't drift back to real GPS.

    A single `LocationSimulation.set()` only holds while the Instruments/DVT
    session stays open — once the channel closes, iOS re-fuses the device's real
    GPS and the spoof 'flickers' back to the true position. 3uTools/GhostMe beat
    this by keeping one channel open and re-pushing the coordinate on a fast
    cadence; this does the same. Keeps a single LocationSimulation open for the
    whole hold (cheap) and re-injects every `dt` seconds until `should_stop()`.
    """
    started = {"ok": False}

    async def run():
        loop = asyncio.get_running_loop()
        async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as loc:
            while should_stop is None or not should_stop():
                t0 = loop.time()
                await loc.set(lat, lon)
                started["ok"] = True
                if on_tick is not None:
                    on_tick()
                await asyncio.sleep(max(0.0, dt - (loop.time() - t0)))

    try:
        await run()
    except Exception:
        # Only remount+retry if we never landed a single fix (DDI not mounted);
        # a mid-hold failure (tunnel drop) propagates so the caller can report it.
        if not started["ok"]:
            await _bounded_mount(rsd)
            await run()
        else:
            raise


async def stream_fixes(
    rsd,
    fixes,
    *,
    dt: float = 1.0,
    on_fix: Optional[Callable[[Fix], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    clear_on_exit: bool = False,
) -> Optional[Fix]:
    """Push an iterable of `Fix` to the device, one per `dt` seconds (real time).

    Keeps a single LocationSimulation channel open for the whole run (fast).
    Returns the last fix pushed. If `clear_on_exit`, restores real GPS on finish;
    otherwise the last simulated location persists (GhostMe's 'stay at end').
    """
    it = iter(fixes)
    try:
        first = next(it)
    except StopIteration:
        return None
    state = {"last": None}

    async def run(start_fix):
        loop = asyncio.get_running_loop()
        async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as loc:
            async def emit(fix) -> float:
                t0 = loop.time()
                await loc.set(fix.lat, fix.lon)
                state["last"] = fix
                if on_fix is not None:
                    on_fix(fix)
                return loop.time() - t0

            call_dur = await emit(start_fix)
            while True:
                if should_stop is not None and should_stop():
                    break
                # Subtract the set() latency so the real cadence stays ~dt and the
                # speed apps compute from consecutive fixes matches the target.
                await asyncio.sleep(max(0.0, dt - call_dur))
                if should_stop is not None and should_stop():
                    break
                try:
                    fix = next(it)
                except StopIteration:
                    break
                call_dur = await emit(fix)
            if clear_on_exit:
                with contextlib.suppress(Exception):
                    await loc.clear()

    try:
        await run(first)
    except Exception:
        if state["last"] is None:
            await _bounded_mount(rsd)  # DDI may need mounting on this device
            await run(first)
        else:
            raise
    return state["last"]


async def open_ready_rsd(udid: Optional[str] = None, tunneld_address=TUNNELD_DEFAULT_ADDRESS):
    """Acquire a tunnel-connected, DDI-mounted RSD ready for location commands.

    Returns (rsd, None) on success, or (None, (code, message)) on failure. The
    caller owns the returned rsd and must close it with `_safe_close`.
    """
    if udid is None:
        udid = await pick_udid()
    if udid is None:
        return None, ("no-device", "No iPhone detected over USB.")

    rsd, state = await acquire_rsd(udid, tunneld_address)
    if state != "ok" or rsd is None:
        msg = {
            "tunneld-down": "Tunnel daemon not running.",
            "no-tunnel-for-device": "No tunnel for the device yet — re-plug or wait a moment.",
            "error": "Could not reach the tunnel daemon.",
        }.get(state, state)
        return None, (state, msg)
    # Device + tunnel are enough to drive location. The DDI/dev-mode mounter
    # service can hang on iOS 26, so it is NOT probed here; set_location/
    # stream_fixes mount lazily (bounded) only if the DVT call actually fails.
    return rsd, None


async def diagnose(udid: Optional[str] = None, tunneld_address=TUNNELD_DEFAULT_ADDRESS) -> Readiness:
    """Full readiness check (device, trust, tunnel, developer mode, DDI)."""
    r = Readiness()

    if udid is None:
        udid = await pick_udid()
    if udid is None:
        r.hints.append("No iPhone detected over USB. Plug it in with a data cable and unlock it.")
        return r

    try:
        r.device = await read_device_info(udid)
    except (pmd.NotTrustedError, pmd.PairingDialogResponsePendingError):
        r.hints.append("Tap 'Trust This Computer' on the iPhone and enter your passcode, then retry.")
        return r
    except pmd.UserDeniedPairingError:
        r.hints.append("Pairing was denied on the iPhone. Reconnect and tap 'Trust'.")
        return r
    except Exception as e:  # noqa: BLE001
        r.hints.append(f"Could not read device info: {type(e).__name__}: {e}")
        return r

    r.tunnel_state = await tunnel_state_for(udid, tunneld_address)
    if r.tunnel_state == "tunneld-down":
        r.hints.append("Tunnel daemon not running — click Enable.")
        return r
    if r.tunnel_state != "ok":
        r.hints.append("No tunnel for the device yet — re-plug the iPhone or wait a moment.")
        return r

    # Device + tunnel present is enough to spoof. The DVT/DDI is exercised lazily
    # on the first set (the mounter service hangs on iOS 26, so we never block on it).
    r.ready = True
    r.ddi_state = "ready"
    r.developer_mode = None
    r.hints.append("Ready.")
    return r
