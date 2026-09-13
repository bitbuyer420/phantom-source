import unittest
from unittest.mock import AsyncMock, patch
from phantom import mover, routing, server


class ValidationTests(unittest.TestCase):
    def test_nonfinite_and_out_of_bounds_coordinates(self):
        for point in [(float("nan"), 1), (0, float("inf")), (91, 0), (0, -181)]:
            with self.assertRaises(ValueError):
                server.coordinates(*point)

    def test_route_count_and_speed(self):
        for pts in [[], [[0, 0]], [[0, 0]] * 51]:
            with self.assertRaises(ValueError):
                server.waypoints_checked(pts)
        for value in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                server.number(value, "speed", .1, 10000)

    def test_pause_preserves_coordinate_progress_and_elapsed(self):
        live = mover.LiveRoute([(0, 0), (1, 0)], speed_mps=5, realistic=True, jitter_m=10)
        live.start_fix()
        with patch.object(mover.time, "monotonic", return_value=100):
            last, _ = live.advance()
        live.paused = True
        with patch.object(mover.time, "monotonic", return_value=1000):
            held, done = live.advance()
        self.assertEqual((held.lat, held.lon, held.distance_m, held.elapsed_s), (last.lat, last.lon, last.distance_m, last.elapsed_s))
        self.assertEqual(held.speed_mps, 0)
        self.assertFalse(done)
        live.paused = False
        with patch.object(mover.time, "monotonic", return_value=1001):
            next_fix, _ = live.advance()
        self.assertLess(next_fix.distance_m - held.distance_m, 10)
        self.assertEqual(next_fix.elapsed_s - held.elapsed_s, 1)


class DwellTests(unittest.TestCase):
    def test_dwell_holds_exact_position_then_finishes(self):
        live = mover.LiveRoute([(0, 0), (.001, 0)], speed_mps=1000, realistic=True, jitter_m=10, dwell_stops=[(10, 2)])
        live.start_fix()
        with patch.object(mover.time, "monotonic", return_value=100):
            stopped, done = live.advance()
        self.assertFalse(done)
        self.assertEqual(stopped.distance_m, 10)
        self.assertEqual(stopped.dwell_remaining_s, 2)
        with patch.object(mover.time, "monotonic", return_value=101):
            held, done = live.advance()
        self.assertEqual((held.lat, held.lon), (stopped.lat, stopped.lon))
        self.assertEqual(held.dwell_remaining_s, 1)
        with patch.object(mover.time, "monotonic", return_value=102):
            held, done = live.advance()
        self.assertFalse(done)
        with patch.object(mover.time, "monotonic", return_value=103):
            final, done = live.advance()
        self.assertTrue(done)
        self.assertEqual(final.progress, 1)


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_global_location_never_resumes(self):
        c = server.ControlSession(AsyncMock())
        c._set = AsyncMock()
        with patch.object(server, "_read_config", return_value={"virtual_lat": 1, "virtual_lon": 2}):
            await c._resume_virtual_if_needed(server.session.Readiness(ready=True), "phone")
        c._set.assert_not_awaited()

    async def test_device_resume_is_opt_in_and_stop_suppresses_it(self):
        c = server.ControlSession(AsyncMock())
        c._set = AsyncMock()
        cfg = {"devices": {"phone": {"virtual_lat": 1, "virtual_lon": 2, "auto_resume": True}}}
        with patch.object(server, "_read_config", return_value=cfg):
            await c._resume_virtual_if_needed(server.session.Readiness(ready=True), "other-phone")
            c._set.assert_not_awaited()
            c.resume_suppressed = True
            await c._resume_virtual_if_needed(server.session.Readiness(ready=True), "phone")
            c._set.assert_not_awaited()

    async def test_routing_failure_preserves_current_session_without_device_access(self):
        c = server.ControlSession(AsyncMock())
        c.cancel_move = AsyncMock()
        with patch.object(routing, "road_route", side_effect=routing.RouteError("offline")), patch.object(server.session, "open_ready_rsd", new=AsyncMock()) as op:
            await c._start_move({"waypoints": [[0, 0], [1, 1]]}, "drive")
        c.cancel_move.assert_not_awaited()
        op.assert_not_awaited()
        self.assertEqual(c.sock.send_json.call_args.args[0]["code"], "routing_failed")

    async def test_route_failure_never_reports_arrival_or_clears_device(self):
        c = server.ControlSession(AsyncMock())
        c.live = mover.LiveRoute([(0, 0), (1, 1)], speed_mps=5)
        with patch.object(server.session, "stream_fixes", side_effect=ConnectionError("lost")), patch.object(server.session, "_safe_close", new=AsyncMock()), patch.object(server.session, "clear_location", new=AsyncMock()) as clear:
            await c._run_move(object(), {"clear_at_end": True})
        events = [a.args[0]["type"] for a in c.sock.send_json.call_args_list]
        self.assertIn("failed", events)
        self.assertNotIn("arrived", events)
        clear.assert_not_awaited()

    async def test_public_router_does_not_pretend_to_walk(self):
        with self.assertRaises(routing.RouteError):
            await routing.road_route([(0, 0), (1, 1)], profile="walking")


class RecoveryIntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_clear_does_not_resume_saved_position(self):
        c = server.ControlSession(AsyncMock())
        c._set = AsyncMock()
        with patch.object(server.session, "open_ready_rsd", new=AsyncMock(return_value=(None, ("offline", "offline")))):
            await c._clear({"udid": "phone"})
        self.assertTrue(c.resume_suppressed)
        with patch.object(server, "device_config", return_value={"auto_resume": True, "virtual_lat": 1, "virtual_lon": 2}):
            await c._resume_virtual_if_needed(server.session.Readiness(ready=True), "phone")
        c._set.assert_not_awaited()

    async def test_failed_set_suppresses_old_position_resume(self):
        c = server.ControlSession(AsyncMock())
        with patch.object(server.session, "open_ready_rsd", new=AsyncMock(return_value=(None, ("offline", "offline")))):
            await c._set({"lat": 1, "lon": 2, "udid": "phone"})
        self.assertTrue(c.resume_suppressed)
        self.assertEqual(c.state["state"], "idle")
