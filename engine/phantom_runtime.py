"""Standalone entry point bundled into Phantom.app.

With no subcommand this starts the loopback-only GUI backend. The ``tunneld``
subcommand runs pymobiledevice3's USB-only RemoteXPC tunnel service for launchd.
"""

from __future__ import annotations

import sys


def run_tunneld(*, check_only: bool = False) -> int:
    from pymobiledevice3.remote.common import TunnelProtocol
    from pymobiledevice3.remote.module_imports import verify_tunnel_imports
    from pymobiledevice3.tunneld.api import TUNNELD_DEFAULT_ADDRESS
    from pymobiledevice3.tunneld.server import TunneldRunner

    if not verify_tunnel_imports():
        return 1
    if check_only:
        return 0
    TunneldRunner.create(
        TUNNELD_DEFAULT_ADDRESS[0],
        TUNNELD_DEFAULT_ADDRESS[1],
        protocol=TunnelProtocol.DEFAULT,
        usb_monitor=True,
        wifi_monitor=False,
        usbmux_monitor=True,
        usbmux_address=None,
        mobdev2_monitor=False,
    )
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "tunneld":
        check_only = len(sys.argv) > 2 and sys.argv[2] == "--check"
        return run_tunneld(check_only=check_only)

    from phantom.server import main as server_main

    return server_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
