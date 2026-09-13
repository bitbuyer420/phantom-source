"""Movement model: turn a route polyline + speed into a stream of GPS fixes.

iOS's location-simulation primitive only accepts a lat/lon pair (no speed,
heading, or timestamp), so *all* realism is produced here on the host by
emitting interpolated coordinates on a timer. One `Fix` is meant to be pushed
to the device per `dt` seconds of real time.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, replace
from typing import Iterator, Sequence

from . import geo

LatLon = tuple[float, float]


@dataclass
class Fix:
    """A single simulated GPS sample."""

    lat: float
    lon: float
    bearing: float          # heading in degrees (for UI / realism only)
    distance_m: float       # cumulative distance travelled so far
    total_m: float          # total route length
    elapsed_s: float        # simulated seconds since start
    speed_mps: float        # instantaneous speed this tick
    dwell_remaining_s: float = 0.0

    @property
    def progress(self) -> float:
        return 0.0 if self.total_m <= 0 else min(1.0, self.distance_m / self.total_m)

    @property
    def eta_s(self) -> float:
        remaining = max(0.0, self.total_m - self.distance_m)
        return remaining / self.speed_mps if self.speed_mps > 0 else 0.0


def _jitter(lat: float, lon: float, jitter_m: float, rng: random.Random) -> LatLon:
    if jitter_m <= 0:
        return lat, lon
    bearing = rng.uniform(0.0, 360.0)
    dist = min(abs(rng.gauss(0.0, jitter_m / 2.0)), jitter_m)
    return geo.destination(lat, lon, bearing, dist)


def build_path(points: Sequence[LatLon], *, round_trip: bool = False) -> list[LatLon]:
    """Normalise waypoints into a path, optionally appending the reverse (A->B->A)."""
    path = [(float(a), float(b)) for a, b in points]
    if not path:
        raise ValueError("route needs at least one point")
    if len(path) == 1:
        path = [path[0], path[0]]
    if round_trip:
        path = path + list(reversed(path))[1:]
    return path


def iter_route_fixes(
    points: Sequence[LatLon],
    speed_mps: float,
    *,
    dt: float = 1.0,
    jitter_m: float = 0.0,
    round_trip: bool = False,
    loop: bool = False,
    realistic: bool = False,
    seed: int | None = None,
) -> Iterator[Fix]:
    """Yield `Fix` samples stepping along `points` at `speed_mps`, one per `dt` seconds.

    - `round_trip`: travel out and back (A->B->A).
    - `loop`: repeat the (possibly round-trip) path forever.
    - `realistic`: vary speed +/-15% each tick and apply `jitter_m` wobble.
    - `jitter_m`: max metres of random positional noise per fix.
    """
    if speed_mps <= 0:
        raise ValueError("speed must be positive")

    base = build_path(points, round_trip=round_trip)
    seg_len = [
        geo.haversine_m(base[k][0], base[k][1], base[k + 1][0], base[k + 1][1])
        for k in range(len(base) - 1)
    ]
    total = sum(seg_len)
    rng = random.Random(seed)

    def one_pass() -> Iterator[Fix]:
        i = 0          # current segment index
        into = 0.0     # metres travelled into current segment
        dist = 0.0     # cumulative metres
        elapsed = 0.0

        def make_fix(speed: float) -> Fix:
            if i >= len(seg_len):
                lat, lon = base[-1]
                brg = geo.bearing_deg(base[-2][0], base[-2][1], base[-1][0], base[-1][1])
            else:
                seg = seg_len[i]
                frac = into / seg if seg > 0 else 0.0
                lat, lon = geo.gc_interpolate(
                    base[i][0], base[i][1], base[i + 1][0], base[i + 1][1], frac
                )
                brg = geo.bearing_deg(base[i][0], base[i][1], base[i + 1][0], base[i + 1][1])
            jlat, jlon = _jitter(lat, lon, jitter_m if realistic else 0.0, rng)
            return Fix(jlat, jlon, brg, dist, total, elapsed, speed)

        # Emit the starting position first.
        yield make_fix(speed_mps)

        while i < len(seg_len):
            speed = speed_mps
            if realistic:
                speed = max(0.1, speed_mps * (1.0 + rng.uniform(-0.15, 0.15)))
            remaining = speed * dt
            elapsed += dt
            while remaining > 0 and i < len(seg_len):
                left = seg_len[i] - into
                if left <= 1e-9:
                    i += 1
                    into = 0.0
                    continue
                if remaining >= left:
                    remaining -= left
                    dist += left
                    i += 1
                    into = 0.0
                else:
                    into += remaining
                    dist += remaining
                    remaining = 0.0
            yield make_fix(speed)

    if loop:
        while True:
            yield from one_pass()
    else:
        yield from one_pass()


class LiveRoute:
    """A route whose speed and path can change while it is being streamed.

    Backs mid-route control: `speed` can be reassigned at any time and `extend()`
    appends new destinations. Each `advance()` moves forward by speed*dt and
    returns the next Fix; consumers read live state every tick.
    """

    def __init__(self, points, *, speed_mps, dt=1.0, jitter_m=0.0,
                 round_trip=False, realistic=False, loop=False, dwell_stops=None):
        self.speed = float(speed_mps)
        self.dt = float(dt)
        self.jitter_m = float(jitter_m)
        self.realistic = bool(realistic)
        self.loop = bool(loop)
        self.paused = False
        self._held_fix = None
        self.cursor = 0.0
        self.elapsed = 0.0
        self._last_t = None  # wall-clock of last advance, for real-time speed
        self._rng = random.Random(0)
        self._set_points(build_path(points, round_trip=round_trip))
        self.dwell_stops = sorted(dwell_stops or [])
        if round_trip:
            self.dwell_stops += [(self.total - d, seconds) for d, seconds in reversed(self.dwell_stops) if d < self.total / 2]
        self._dwell_index = 0
        self.dwell_remaining = 0.0

    def _set_points(self, pts) -> None:
        self.path = [(float(a), float(b)) for a, b in pts]
        self.seg = [
            geo.haversine_m(self.path[i][0], self.path[i][1], self.path[i + 1][0], self.path[i + 1][1])
            for i in range(len(self.path) - 1)
        ]
        self.total = sum(self.seg)

    def extend(self, new_points) -> None:
        """Append destinations after the current end of the path (live)."""
        prev = self.path[-1]
        for p in new_points:
            pt = (float(p[0]), float(p[1]))
            self.seg.append(geo.haversine_m(prev[0], prev[1], pt[0], pt[1]))
            self.path.append(pt)
            self.total += self.seg[-1]
            prev = pt

    def _point_at(self, dist: float):
        if not self.seg:
            return self.path[0], 0.0
        if dist <= 0:
            return self.path[0], geo.bearing_deg(self.path[0][0], self.path[0][1], self.path[1][0], self.path[1][1])
        acc = 0.0
        for i, s in enumerate(self.seg):
            if acc + s >= dist:
                f = (dist - acc) / s if s > 0 else 0.0
                lat, lon = geo.gc_interpolate(self.path[i][0], self.path[i][1], self.path[i + 1][0], self.path[i + 1][1], f)
                brg = geo.bearing_deg(self.path[i][0], self.path[i][1], self.path[i + 1][0], self.path[i + 1][1])
                return (lat, lon), brg
            acc += s
        last = self.path[-1]
        return last, geo.bearing_deg(self.path[-2][0], self.path[-2][1], last[0], last[1])

    def _fix(self, speed: float) -> Fix:
        (lat, lon), brg = self._point_at(self.cursor)
        if self.realistic and self.jitter_m > 0 and not self.dwell_remaining:
            lat, lon = _jitter(lat, lon, self.jitter_m, self._rng)
        return Fix(lat, lon, brg, min(self.cursor, self.total), self.total, self.elapsed, speed, self.dwell_remaining)

    def start_fix(self) -> Fix:
        if self.dwell_stops and self.dwell_stops[0][0] == 0:
            self.dwell_remaining = self.dwell_stops[0][1]
            self._dwell_index = 1
        self._held_fix = self._fix(0 if self.dwell_remaining else self.speed)
        return self._held_fix

    def advance(self):
        """Advance by the REAL elapsed time since the last tick so the ground
        speed apps derive from GPS deltas matches the target exactly, immune to
        timer jitter / set() latency. Returns (Fix, done)."""
        now = time.monotonic()
        if self.paused:
            self._last_t = now
            return replace(self._held_fix or self.start_fix(), speed_mps=0), False
        real_dt = self.dt if self._last_t is None else (now - self._last_t)
        real_dt = min(real_dt, self.dt * 5.0)  # cap a stalled tick so it can't teleport
        self._last_t = now
        if self.dwell_remaining > 0:
            self.dwell_remaining = max(0, self.dwell_remaining - real_dt)
            self.elapsed += real_dt
            # Keep the exact stopped coordinate even for the tick that ends the dwell.
            f = self._held_fix
            self._held_fix = Fix(f.lat, f.lon, f.bearing, f.distance_m, self.total, self.elapsed, 0, self.dwell_remaining)
            return self._held_fix, False
        speed = self.speed
        if self.realistic:
            speed = max(0.1, self.speed * (1.0 + self._rng.uniform(-0.15, 0.15)))
        target = self.cursor + speed * real_dt
        if self._dwell_index < len(self.dwell_stops):
            distance, seconds = self.dwell_stops[self._dwell_index]
            if self.cursor <= distance <= target:
                self.cursor = distance
                self.elapsed += real_dt
                self.dwell_remaining = seconds
                self._dwell_index += 1
                self._held_fix = self._fix(0)
                return self._held_fix, False
        self.cursor = target
        self.elapsed += real_dt
        done = False
        if self.cursor >= self.total:
            if self.loop:
                self.cursor = 0.0
                self._dwell_index = 0
            else:
                self.cursor = self.total
                done = True
        self._held_fix = self._fix(speed)
        return self._held_fix, done


def live_fix_stream(live: LiveRoute, should_stop=None):
    """Yield fixes from a LiveRoute, re-reading its (mutable) speed/path each tick."""
    yield live.start_fix()
    while True:
        if should_stop is not None and should_stop():
            return
        fix, done = live.advance()
        yield fix
        if done:
            return
