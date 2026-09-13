from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from phantom import geo, mover, server


class MovementTests(unittest.TestCase):
    def test_route_reaches_destination(self) -> None:
        points = [(41.2565, -95.9345), (41.2575, -95.9330)]
        fixes = list(mover.iter_route_fixes(points, speed_mps=12.0, dt=0.5, seed=1))
        self.assertGreater(len(fixes), 2)
        self.assertAlmostEqual(fixes[-1].lat, points[-1][0], places=5)
        self.assertAlmostEqual(fixes[-1].lon, points[-1][1], places=5)
        self.assertAlmostEqual(fixes[-1].progress, 1.0, places=6)

    def test_live_speed_can_change(self) -> None:
        live = mover.LiveRoute([(41.0, -96.0), (41.1, -96.0)], speed_mps=5.0, dt=1.0)
        live.start_fix()
        live.speed = 20.0
        fix, done = live.advance()
        self.assertFalse(done)
        self.assertEqual(fix.speed_mps, 20.0)
        self.assertGreater(fix.distance_m, 0.0)

    def test_geodesic_distance_is_symmetric(self) -> None:
        a = geo.haversine_m(41.2565, -95.9345, 40.7128, -74.0060)
        b = geo.haversine_m(40.7128, -74.0060, 41.2565, -95.9345)
        self.assertAlmostEqual(a, b, places=6)
        self.assertGreater(a, 1_000_000)


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_location_resumes_when_device_is_ready(self) -> None:
        control = server.ControlSession(AsyncMock())
        control.send = AsyncMock()
        control._set = AsyncMock()
        readiness = server.session.Readiness(ready=True)
        with patch.object(server, "_read_config", return_value={"devices": {"test-udid": {"virtual_lat": 41.2, "virtual_lon": -96.0, "auto_resume": True}}}):
            await control._resume_virtual_if_needed(readiness, "test-udid")
        control._set.assert_awaited_once_with({
            "lat": 41.2,
            "lon": -96.0,
            "udid": "test-udid",
            "name": "Persistent location",
        })

    async def test_static_hold_reacquires_tunnel_after_drop(self) -> None:
        control = server.ControlSession(AsyncMock())
        control.send = AsyncMock()
        first, recovered = object(), object()

        async def hold(rsd, lat, lon, *, dt, should_stop, on_tick=None):
            if rsd is first:
                raise ConnectionError("simulated tunnel drop")
            control.hold_stop.set()

        with (
            patch.object(server.session, "hold_location", side_effect=hold) as hold_mock,
            patch.object(server.session, "open_ready_rsd", new=AsyncMock(return_value=(recovered, None))) as open_mock,
            patch.object(server.session, "_safe_close", new=AsyncMock()) as close_mock,
        ):
            await control._run_hold(first, 41.2, -96.0, 0.01, udid="test-udid")

        self.assertEqual(hold_mock.await_count, 2)
        open_mock.assert_awaited_once_with("test-udid")
        self.assertEqual(close_mock.await_count, 2)


class PackagingTests(unittest.TestCase):
    def test_packaged_tunnel_plist_uses_bundled_runtime(self) -> None:
        runtime = "/Applications/Phantom.app/Contents/Resources/bin/phantom-runtime/phantom-runtime"
        with patch.dict(os.environ, {"PHANTOM_RUNTIME_EXECUTABLE": runtime}):
            xml = server._tunneld_plist_xml()
        self.assertIn(f"<string>{server.PRIVILEGED_RUNTIME_EXECUTABLE}</string>", xml)
        self.assertNotIn(f"<string>{runtime}</string>", xml)
        self.assertIn("<string>tunneld</string>", xml)
        self.assertNotIn("-m</string>", xml)

    def test_control_plane_rejects_non_loopback(self) -> None:
        with self.assertRaises(SystemExit):
            server.run("0.0.0.0", 8765)


if __name__ == "__main__":
    unittest.main()
